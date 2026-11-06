<!-- SPDX-License-Identifier: Apache-2.0 -->

<!--
Noise-floor and coverage summary over run 42174388801's store, rendered by `tests/evals/noise_floor.py` inside Actions where the artifact is reachable. Section 1-2 are the floor the higher-tier conditions in `routed-section-arm-2026-11-06.md` are read against -- same store, same model, same grader. Section 6 names the 54 cells the partial run did not reach.
-->

# North-star grid noise floor (athenaeum#2309)

- run_id: 42174388801
- generated: 2026-11-06T07:23:34Z
- model: claude-sonnet-5
- search_backend: vector
- grader_revision: athenaeum#1935 (code computing THIS report, not the rows)
- git_sha: 531fe48d9aaa (code computing THIS report, not the rows)
- athenaeum_version: 0.23.4
- corpus_digest[core]: 3c5ea876688b089e
- rows: 810 total, 804 gradable, 6 harness failures, 0 torn, 0 duplicate

## 1. Replicate-to-replicate variance

| probe_class | scale | arm | n (rep0/rep1) | rate rep0 | rate rep1 | n paired | discordance | SD(class rate) | df | estimator |
|---|---|---|---|---|---|---|---|---|---|---|
| abstention | core | native_grep | 3/3 | 1.000 | 1.000 | 3 | 0.000 | 0.000 | 2 | paired discordance |
| abstention | core | native_index | 3/3 | 0.667 | 0.000 | 3 | 0.667 | 0.192 | 2 | paired discordance |
| abstention | core | none | 3/3 | 1.000 | 1.000 | 3 | 0.000 | 0.000 | 2 | paired discordance |
| abstention | core | oracle | 3/3 | 1.000 | 1.000 | 3 | 0.000 | 0.000 | 2 | paired discordance |
| abstention | core | pull | 3/3 | 1.000 | 1.000 | 3 | 0.000 | 0.000 | 2 | paired discordance |
| abstention | core | push_breadcrumb | 3/3 | 1.000 | 1.000 | 3 | 0.000 | 0.000 | 2 | paired discordance |
| abstention | core | push_breadcrumb_pull | 3/3 | 0.333 | 0.667 | 3 | 0.333 | 0.192 | 2 | paired discordance |
| abstention | core | push_pages_upper_bound | 3/3 | 1.000 | 1.000 | 3 | 0.000 | 0.000 | 2 | paired discordance |
| abstention | core | push_sections_pull | 3/3 | 0.667 | 1.000 | 3 | 0.333 | 0.192 | 2 | paired discordance |
| disambiguation | core | native_grep | 4/3 | 0.500 | 0.667 | 3 | 0.333 | 0.192 | 2 | paired discordance |
| disambiguation | core | native_index | 4/3 | 1.000 | 1.000 | 3 | 0.000 | 0.000 | 2 | paired discordance |
| disambiguation | core | none | 4/3 | 0.000 | 0.000 | 3 | 0.000 | 0.000 | 2 | paired discordance |
| disambiguation | core | oracle | 4/3 | 1.000 | 1.000 | 3 | 0.000 | 0.000 | 2 | paired discordance |
| disambiguation | core | pull | 4/3 | 1.000 | 1.000 | 3 | 0.000 | 0.000 | 2 | paired discordance |
| disambiguation | core | push_breadcrumb | 4/3 | 0.000 | 0.000 | 3 | 0.000 | 0.000 | 2 | paired discordance |
| disambiguation | core | push_breadcrumb_pull | 4/3 | 1.000 | 1.000 | 3 | 0.000 | 0.000 | 2 | paired discordance |
| disambiguation | core | push_pages_upper_bound | 4/3 | 1.000 | 1.000 | 3 | 0.000 | 0.000 | 2 | paired discordance |
| disambiguation | core | push_sections_pull | 4/3 | 1.000 | 1.000 | 3 | 0.000 | 0.000 | 2 | paired discordance |
| distractor_robustness | core | native_grep | 2/2 | 0.500 | 1.000 | 2 | 0.500 | 0.250 | 1 | paired discordance |
| distractor_robustness | core | native_index | 2/2 | 0.500 | 1.000 | 2 | 0.500 | 0.250 | 1 | paired discordance |
| distractor_robustness | core | none | 2/2 | 0.000 | 0.000 | 2 | 0.000 | 0.000 | 1 | paired discordance |
| distractor_robustness | core | oracle | 2/2 | 1.000 | 1.000 | 2 | 0.000 | 0.000 | 1 | paired discordance |
| distractor_robustness | core | pull | 2/2 | 1.000 | 1.000 | 2 | 0.000 | 0.000 | 1 | paired discordance |
| distractor_robustness | core | push_breadcrumb | 2/2 | 0.000 | 0.000 | 2 | 0.000 | 0.000 | 1 | paired discordance |
| distractor_robustness | core | push_breadcrumb_pull | 2/2 | 1.000 | 0.500 | 2 | 0.500 | 0.250 | 1 | paired discordance |
| distractor_robustness | core | push_pages_upper_bound | 2/2 | 1.000 | 1.000 | 2 | 0.000 | 0.000 | 1 | paired discordance |
| distractor_robustness | core | push_sections_pull | 2/2 | 1.000 | 1.000 | 2 | 0.000 | 0.000 | 1 | paired discordance |
| follow_through | core | native_grep | 6/6 | 0.833 | 1.000 | 6 | 0.167 | 0.108 | 5 | paired discordance |
| follow_through | core | native_index | 6/6 | 1.000 | 1.000 | 6 | 0.000 | 0.000 | 5 | paired discordance |
| follow_through | core | none | 6/6 | 0.000 | 0.000 | 6 | 0.000 | 0.000 | 5 | paired discordance |
| follow_through | core | oracle | 6/6 | 1.000 | 0.833 | 6 | 0.167 | 0.108 | 5 | paired discordance |
| follow_through | core | pull | 6/6 | 1.000 | 1.000 | 6 | 0.000 | 0.000 | 5 | paired discordance |
| follow_through | core | push_breadcrumb | 6/6 | 0.000 | 0.000 | 6 | 0.000 | 0.000 | 5 | paired discordance |
| follow_through | core | push_breadcrumb_pull | 6/6 | 0.667 | 0.833 | 6 | 0.167 | 0.108 | 5 | paired discordance |
| follow_through | core | push_pages_upper_bound | 6/6 | 0.000 | 0.000 | 6 | 0.000 | 0.000 | 5 | paired discordance |
| follow_through | core | push_sections_pull | 6/6 | 1.000 | 1.000 | 6 | 0.000 | 0.000 | 5 | paired discordance |
| multi_hop | core | native_grep | 1/2 | 0.000 | 0.000 | 1 | 0.000 | 0.000 | 0 | paired discordance |
| multi_hop | core | native_index | 3/3 | 0.333 | 0.333 | 3 | 0.000 | 0.000 | 2 | paired discordance |
| multi_hop | core | none | 3/3 | 0.000 | 0.000 | 3 | 0.000 | 0.000 | 2 | paired discordance |
| multi_hop | core | oracle | 3/3 | 1.000 | 0.667 | 3 | 0.333 | 0.192 | 2 | paired discordance |
| multi_hop | core | pull | 3/3 | 0.333 | 0.333 | 3 | 0.000 | 0.000 | 2 | paired discordance |
| multi_hop | core | push_breadcrumb | 3/3 | 0.000 | 0.000 | 3 | 0.000 | 0.000 | 2 | paired discordance |
| multi_hop | core | push_breadcrumb_pull | 3/2 | 0.333 | 0.000 | 2 | 0.000 | 0.000 | 1 | paired discordance |
| multi_hop | core | push_pages_upper_bound | 3/3 | 0.000 | 0.000 | 3 | 0.000 | 0.000 | 2 | paired discordance |
| multi_hop | core | push_sections_pull | 3/3 | 0.333 | 0.333 | 3 | 0.000 | 0.000 | 2 | paired discordance |
| redundancy | core | native_grep | 4/4 | 0.500 | 0.500 | 4 | 0.000 | 0.000 | 3 | paired discordance |
| redundancy | core | native_index | 4/4 | 0.750 | 0.750 | 4 | 0.000 | 0.000 | 3 | paired discordance |
| redundancy | core | none | 4/4 | 0.000 | 0.000 | 4 | 0.000 | 0.000 | 3 | paired discordance |
| redundancy | core | oracle | 4/4 | 0.750 | 0.750 | 4 | 0.000 | 0.000 | 3 | paired discordance |
| redundancy | core | pull | 4/4 | 0.750 | 0.500 | 4 | 0.250 | 0.153 | 3 | paired discordance |
| redundancy | core | push_breadcrumb | 4/4 | 0.000 | 0.000 | 4 | 0.000 | 0.000 | 3 | paired discordance |
| redundancy | core | push_breadcrumb_pull | 4/4 | 0.500 | 0.500 | 4 | 0.000 | 0.000 | 3 | paired discordance |
| redundancy | core | push_pages_upper_bound | 4/4 | 0.750 | 0.750 | 4 | 0.000 | 0.000 | 3 | paired discordance |
| redundancy | core | push_sections_pull | 4/4 | 0.750 | 0.750 | 4 | 0.000 | 0.000 | 3 | paired discordance |
| single_hop | core | native_grep | 7/8 | 1.000 | 1.000 | 7 | 0.000 | 0.000 | 6 | paired discordance |
| single_hop | core | native_index | 7/8 | 0.714 | 0.875 | 7 | 0.143 | 0.094 | 6 | paired discordance |
| single_hop | core | none | 7/8 | 0.000 | 0.000 | 7 | 0.000 | 0.000 | 6 | paired discordance |
| single_hop | core | oracle | 7/8 | 1.000 | 0.875 | 7 | 0.000 | 0.000 | 6 | paired discordance |
| single_hop | core | pull | 7/8 | 1.000 | 1.000 | 7 | 0.000 | 0.000 | 6 | paired discordance |
| single_hop | core | push_breadcrumb | 7/8 | 0.000 | 0.000 | 7 | 0.000 | 0.000 | 6 | paired discordance |
| single_hop | core | push_breadcrumb_pull | 7/8 | 1.000 | 1.000 | 7 | 0.000 | 0.000 | 6 | paired discordance |
| single_hop | core | push_pages_upper_bound | 7/8 | 0.571 | 0.500 | 7 | 0.000 | 0.000 | 6 | paired discordance |
| single_hop | core | push_sections_pull | 7/8 | 1.000 | 1.000 | 7 | 0.000 | 0.000 | 6 | paired discordance |
| temporal | core | native_grep | 6/6 | 1.000 | 1.000 | 6 | 0.000 | 0.000 | 5 | paired discordance |
| temporal | core | native_index | 6/6 | 0.833 | 0.833 | 6 | 0.333 | 0.167 | 5 | paired discordance |
| temporal | core | none | 6/6 | 0.000 | 0.000 | 6 | 0.000 | 0.000 | 5 | paired discordance |
| temporal | core | oracle | 6/6 | 1.000 | 1.000 | 6 | 0.000 | 0.000 | 5 | paired discordance |
| temporal | core | pull | 6/6 | 1.000 | 0.833 | 6 | 0.167 | 0.108 | 5 | paired discordance |
| temporal | core | push_breadcrumb | 6/6 | 0.000 | 0.000 | 6 | 0.000 | 0.000 | 5 | paired discordance |
| temporal | core | push_breadcrumb_pull | 6/6 | 0.833 | 0.833 | 6 | 0.000 | 0.000 | 5 | paired discordance |
| temporal | core | push_pages_upper_bound | 6/6 | 1.000 | 1.000 | 6 | 0.000 | 0.000 | 5 | paired discordance |
| temporal | core | push_sections_pull | 6/6 | 1.000 | 1.000 | 6 | 0.000 | 0.000 | 5 | paired discordance |
| unprompted_push | core | native_grep | 8/10 | 0.250 | 0.400 | 8 | 0.125 | 0.083 | 7 | paired discordance |
| unprompted_push | core | native_index | 10/10 | 0.400 | 0.400 | 9 | 0.333 | 0.134 | 8 | paired discordance |
| unprompted_push | core | none | 10/10 | 0.000 | 0.000 | 9 | 0.000 | 0.000 | 8 | paired discordance |
| unprompted_push | core | oracle | 10/10 | 0.300 | 0.600 | 9 | 0.222 | 0.098 | 8 | paired discordance |
| unprompted_push | core | pull | 10/10 | 0.300 | 0.200 | 9 | 0.222 | 0.098 | 8 | paired discordance |
| unprompted_push | core | push_breadcrumb | 10/10 | 0.000 | 0.000 | 9 | 0.000 | 0.000 | 8 | paired discordance |
| unprompted_push | core | push_breadcrumb_pull | 10/10 | 0.300 | 0.400 | 9 | 0.111 | 0.074 | 8 | paired discordance |
| unprompted_push | core | push_pages_upper_bound | 10/10 | 0.000 | 0.000 | 9 | 0.000 | 0.000 | 8 | paired discordance |
| unprompted_push | core | push_sections_pull | 10/10 | 0.600 | 0.600 | 9 | 0.222 | 0.111 | 8 | paired discordance |

## 2. Minimum detectable difference vs `push_breadcrumb_pull` (alpha=0.05, power=0.80)

| arm | probe_class | scale | n | rate(arm) | rate(verdict) | MDE paired | MDE unpaired | normal approx reliable |
|---|---|---|---|---|---|---|---|---|
| native_grep | abstention | core | 6 | 1.000 | 0.500 | 0.572 (insufficient n for normal approx -- use exact test) | 0.700 | False |
| native_index | abstention | core | 6 | 0.333 | 0.500 | 0.786 (insufficient n for normal approx -- use exact test) | 0.797 | False |
| none | abstention | core | 6 | 1.000 | 0.500 | 0.572 (insufficient n for normal approx -- use exact test) | 0.700 | False |
| oracle | abstention | core | 6 | 1.000 | 0.500 | 0.572 (insufficient n for normal approx -- use exact test) | 0.700 | False |
| pull | abstention | core | 6 | 1.000 | 0.500 | 0.572 (insufficient n for normal approx -- use exact test) | 0.700 | False |
| push_breadcrumb | abstention | core | 6 | 1.000 | 0.500 | 0.572 (insufficient n for normal approx -- use exact test) | 0.700 | False |
| push_pages_upper_bound | abstention | core | 6 | 1.000 | 0.500 | 0.572 (insufficient n for normal approx -- use exact test) | 0.700 | False |
| push_sections_pull | abstention | core | 6 | 0.833 | 0.500 | 0.539 (insufficient n for normal approx -- use exact test) | 0.762 | False |
| native_grep | disambiguation | core | 7 | 0.571 | 1.000 | 0.524 (insufficient n for normal approx -- use exact test) | 0.614 | False |
| native_index | disambiguation | core | 7 | 1.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.000 | False |
| none | disambiguation | core | 7 | 0.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.749 | False |
| oracle | disambiguation | core | 7 | 1.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.000 | False |
| pull | disambiguation | core | 7 | 1.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.000 | False |
| push_breadcrumb | disambiguation | core | 7 | 0.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.749 | False |
| push_pages_upper_bound | disambiguation | core | 7 | 1.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.000 | False |
| push_sections_pull | disambiguation | core | 7 | 1.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.000 | False |
| native_grep | distractor_robustness | core | 4 | 0.750 | 0.750 | 0.991 (insufficient n for normal approx -- use exact test) | 0.858 | False |
| native_index | distractor_robustness | core | 4 | 0.750 | 0.750 | 0.991 (insufficient n for normal approx -- use exact test) | 0.858 | False |
| none | distractor_robustness | core | 4 | 0.000 | 0.750 | 0.607 (insufficient n for normal approx -- use exact test) | 0.959 | False |
| oracle | distractor_robustness | core | 4 | 1.000 | 0.750 | 0.607 (insufficient n for normal approx -- use exact test) | 0.655 | False |
| pull | distractor_robustness | core | 4 | 1.000 | 0.750 | 0.607 (insufficient n for normal approx -- use exact test) | 0.655 | False |
| push_breadcrumb | distractor_robustness | core | 4 | 0.000 | 0.750 | 0.607 (insufficient n for normal approx -- use exact test) | 0.959 | False |
| push_pages_upper_bound | distractor_robustness | core | 4 | 1.000 | 0.750 | 0.607 (insufficient n for normal approx -- use exact test) | 0.655 | False |
| push_sections_pull | distractor_robustness | core | 4 | 1.000 | 0.750 | 0.607 (insufficient n for normal approx -- use exact test) | 0.655 | False |
| native_grep | follow_through | core | 12 | 0.917 | 0.750 | 0.301 (insufficient n for normal approx -- use exact test) | 0.426 | False |
| native_index | follow_through | core | 12 | 1.000 | 0.750 | 0.350 (insufficient n for normal approx -- use exact test) | 0.378 | False |
| none | follow_through | core | 12 | 0.000 | 0.750 | 0.350 (insufficient n for normal approx -- use exact test) | 0.554 | False |
| oracle | follow_through | core | 12 | 0.917 | 0.750 | 0.301 (insufficient n for normal approx -- use exact test) | 0.426 | False |
| pull | follow_through | core | 12 | 1.000 | 0.750 | 0.350 (insufficient n for normal approx -- use exact test) | 0.378 | False |
| push_breadcrumb | follow_through | core | 12 | 0.000 | 0.750 | 0.350 (insufficient n for normal approx -- use exact test) | 0.554 | False |
| push_pages_upper_bound | follow_through | core | 12 | 0.000 | 0.750 | 0.350 (insufficient n for normal approx -- use exact test) | 0.554 | False |
| push_sections_pull | follow_through | core | 12 | 1.000 | 0.750 | 0.350 (insufficient n for normal approx -- use exact test) | 0.378 | False |
| native_grep | multi_hop | core | 3 | 0.000 | 0.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.000 | False |
| native_index | multi_hop | core | 5 | 0.200 | 0.200 | 0.000 (insufficient n for normal approx -- use exact test) | 0.709 | False |
| none | multi_hop | core | 5 | 0.000 | 0.200 | 0.501 (insufficient n for normal approx -- use exact test) | 0.532 | False |
| oracle | multi_hop | core | 5 | 0.800 | 0.200 | 0.614 (insufficient n for normal approx -- use exact test) | 0.886 | False |
| pull | multi_hop | core | 5 | 0.200 | 0.200 | 0.000 (insufficient n for normal approx -- use exact test) | 0.709 | False |
| push_breadcrumb | multi_hop | core | 5 | 0.000 | 0.200 | 0.501 (insufficient n for normal approx -- use exact test) | 0.532 | False |
| push_pages_upper_bound | multi_hop | core | 5 | 0.000 | 0.200 | 0.501 (insufficient n for normal approx -- use exact test) | 0.532 | False |
| push_sections_pull | multi_hop | core | 5 | 0.200 | 0.200 | 0.000 (insufficient n for normal approx -- use exact test) | 0.709 | False |
| native_grep | redundancy | core | 8 | 0.500 | 0.500 | 0.700 (insufficient n for normal approx -- use exact test) | 0.700 | False |
| native_index | redundancy | core | 8 | 0.750 | 0.500 | 0.429 (insufficient n for normal approx -- use exact test) | 0.678 | False |
| none | redundancy | core | 8 | 0.000 | 0.500 | 0.495 (insufficient n for normal approx -- use exact test) | 0.607 | False |
| oracle | redundancy | core | 8 | 0.750 | 0.500 | 0.429 (insufficient n for normal approx -- use exact test) | 0.678 | False |
| pull | redundancy | core | 8 | 0.625 | 0.500 | 0.328 (insufficient n for normal approx -- use exact test) | 0.695 | False |
| push_breadcrumb | redundancy | core | 8 | 0.000 | 0.500 | 0.495 (insufficient n for normal approx -- use exact test) | 0.607 | False |
| push_pages_upper_bound | redundancy | core | 8 | 0.750 | 0.500 | 0.429 (insufficient n for normal approx -- use exact test) | 0.678 | False |
| push_sections_pull | redundancy | core | 8 | 0.750 | 0.500 | 0.429 (insufficient n for normal approx -- use exact test) | 0.678 | False |
| native_grep | single_hop | core | 15 | 1.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.000 | False |
| native_index | single_hop | core | 15 | 0.800 | 1.000 | 0.289 (insufficient n for normal approx -- use exact test) | 0.307 | False |
| none | single_hop | core | 15 | 0.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.511 | False |
| oracle | single_hop | core | 15 | 0.933 | 1.000 | 0.180 (insufficient n for normal approx -- use exact test) | 0.184 | False |
| pull | single_hop | core | 15 | 1.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.000 | False |
| push_breadcrumb | single_hop | core | 15 | 0.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.511 | False |
| push_pages_upper_bound | single_hop | core | 15 | 0.533 | 1.000 | 0.361 | 0.433 | True |
| push_sections_pull | single_hop | core | 15 | 1.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.000 | False |
| native_grep | temporal | core | 12 | 1.000 | 0.833 | 0.301 (insufficient n for normal approx -- use exact test) | 0.316 | False |
| native_index | temporal | core | 12 | 0.833 | 0.833 | 0.330 (insufficient n for normal approx -- use exact test) | 0.426 | False |
| none | temporal | core | 12 | 0.000 | 0.833 | 0.301 (insufficient n for normal approx -- use exact test) | 0.564 | False |
| oracle | temporal | core | 12 | 1.000 | 0.833 | 0.301 (insufficient n for normal approx -- use exact test) | 0.316 | False |
| pull | temporal | core | 12 | 0.917 | 0.833 | 0.224 (insufficient n for normal approx -- use exact test) | 0.378 | False |
| push_breadcrumb | temporal | core | 12 | 0.000 | 0.833 | 0.301 (insufficient n for normal approx -- use exact test) | 0.564 | False |
| push_pages_upper_bound | temporal | core | 12 | 1.000 | 0.833 | 0.301 (insufficient n for normal approx -- use exact test) | 0.316 | False |
| push_sections_pull | temporal | core | 12 | 1.000 | 0.833 | 0.301 (insufficient n for normal approx -- use exact test) | 0.316 | False |
| native_grep | unprompted_push | core | 18 | 0.333 | 0.333 | 0.000 (insufficient n for normal approx -- use exact test) | 0.440 | False |
| native_index | unprompted_push | core | 20 | 0.400 | 0.350 | 0.312 | 0.429 | True |
| none | unprompted_push | core | 20 | 0.000 | 0.350 | 0.299 | 0.337 | True |
| oracle | unprompted_push | core | 20 | 0.450 | 0.350 | 0.273 (insufficient n for normal approx -- use exact test) | 0.434 | False |
| pull | unprompted_push | core | 20 | 0.250 | 0.350 | 0.391 | 0.406 | True |
| push_breadcrumb | unprompted_push | core | 20 | 0.000 | 0.350 | 0.299 | 0.337 | True |
| push_pages_upper_bound | unprompted_push | core | 20 | 0.000 | 0.350 | 0.299 | 0.337 | True |
| push_sections_pull | unprompted_push | core | 20 | 0.600 | 0.350 | 0.271 | 0.442 | True |

Formulas: paired/McNemar: MDE = (z_a/2 + z_b) * sqrt(p_disc - (p10-p01)^2) / sqrt(n), alpha=0.05 two-sided, power=0.80

Formulas: unpaired two-proportion normal approximation (pooled variance at the mean rate, SAME n per side, pairing structure discarded): MDE = (z_a/2 + z_b) * sqrt(2 * pbar * (1-pbar)) / sqrt(n)

## 3. Required n to detect 0.05 / 0.10 / 0.15 / 0.20

| arm | probe_class | scale | n achieved | n@0.05 | n@0.10 | n@0.15 | n@0.20 |
|---|---|---|---|---|---|---|---|
| native_grep | abstention | core | 6 | 785 | 197 | 88 | 50 |
| native_index | abstention | core | 6 | 1483 | 371 | 165 | 93 |
| none | abstention | core | 6 | 785 | 197 | 88 | 50 |
| oracle | abstention | core | 6 | 785 | 197 | 88 | 50 |
| pull | abstention | core | 6 | 785 | 197 | 88 | 50 |
| push_breadcrumb | abstention | core | 6 | 785 | 197 | 88 | 50 |
| push_pages_upper_bound | abstention | core | 6 | 785 | 197 | 88 | 50 |
| push_sections_pull | abstention | core | 6 | 698 | 175 | 78 | 44 |
| native_grep | disambiguation | core | 7 | 769 | 193 | 86 | 49 |
| native_index | disambiguation | core | 7 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| none | disambiguation | core | 7 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| oracle | disambiguation | core | 7 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| pull | disambiguation | core | 7 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| push_breadcrumb | disambiguation | core | 7 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| push_pages_upper_bound | disambiguation | core | 7 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| push_sections_pull | disambiguation | core | 7 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| native_grep | distractor_robustness | core | 4 | 1570 | 393 | 175 | 99 |
| native_index | distractor_robustness | core | 4 | 1570 | 393 | 175 | 99 |
| none | distractor_robustness | core | 4 | 589 | 148 | 66 | 37 |
| oracle | distractor_robustness | core | 4 | 589 | 148 | 66 | 37 |
| pull | distractor_robustness | core | 4 | 589 | 148 | 66 | 37 |
| push_breadcrumb | distractor_robustness | core | 4 | 589 | 148 | 66 | 37 |
| push_pages_upper_bound | distractor_robustness | core | 4 | 589 | 148 | 66 | 37 |
| push_sections_pull | distractor_robustness | core | 4 | 589 | 148 | 66 | 37 |
| native_grep | follow_through | core | 12 | 437 | 110 | 49 | 28 |
| native_index | follow_through | core | 12 | 589 | 148 | 66 | 37 |
| none | follow_through | core | 12 | 589 | 148 | 66 | 37 |
| oracle | follow_through | core | 12 | 437 | 110 | 49 | 28 |
| pull | follow_through | core | 12 | 589 | 148 | 66 | 37 |
| push_breadcrumb | follow_through | core | 12 | 589 | 148 | 66 | 37 |
| push_pages_upper_bound | follow_through | core | 12 | 589 | 148 | 66 | 37 |
| push_sections_pull | follow_through | core | 12 | 589 | 148 | 66 | 37 |
| native_grep | multi_hop | core | 3 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| native_index | multi_hop | core | 5 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| none | multi_hop | core | 5 | 503 | 126 | 56 | 32 |
| oracle | multi_hop | core | 5 | 754 | 189 | 84 | 48 |
| pull | multi_hop | core | 5 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| push_breadcrumb | multi_hop | core | 5 | 503 | 126 | 56 | 32 |
| push_pages_upper_bound | multi_hop | core | 5 | 503 | 126 | 56 | 32 |
| push_sections_pull | multi_hop | core | 5 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| native_grep | redundancy | core | 8 | 1570 | 393 | 175 | 99 |
| native_index | redundancy | core | 8 | 589 | 148 | 66 | 37 |
| none | redundancy | core | 8 | 785 | 197 | 88 | 50 |
| oracle | redundancy | core | 8 | 589 | 148 | 66 | 37 |
| pull | redundancy | core | 8 | 344 | 86 | 39 | 22 |
| push_breadcrumb | redundancy | core | 8 | 785 | 197 | 88 | 50 |
| push_pages_upper_bound | redundancy | core | 8 | 589 | 148 | 66 | 37 |
| push_sections_pull | redundancy | core | 8 | 589 | 148 | 66 | 37 |
| native_grep | single_hop | core | 15 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| native_index | single_hop | core | 15 | 503 | 126 | 56 | 32 |
| none | single_hop | core | 15 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| oracle | single_hop | core | 15 | 196 | 49 | 22 | 13 |
| pull | single_hop | core | 15 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| push_breadcrumb | single_hop | core | 15 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| push_pages_upper_bound | single_hop | core | 15 | 782 | 196 | 87 | 49 |
| push_sections_pull | single_hop | core | 15 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| native_grep | temporal | core | 12 | 437 | 110 | 49 | 28 |
| native_index | temporal | core | 12 | 524 | 131 | 59 | 33 |
| none | temporal | core | 12 | 437 | 110 | 49 | 28 |
| oracle | temporal | core | 12 | 437 | 110 | 49 | 28 |
| pull | temporal | core | 12 | 240 | 60 | 27 | 15 |
| push_breadcrumb | temporal | core | 12 | 437 | 110 | 49 | 28 |
| push_pages_upper_bound | temporal | core | 12 | 437 | 110 | 49 | 28 |
| push_sections_pull | temporal | core | 12 | 437 | 110 | 49 | 28 |
| native_grep | unprompted_push | core | 18 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| native_index | unprompted_push | core | 20 | 778 | 195 | 87 | 49 |
| none | unprompted_push | core | 20 | 715 | 179 | 80 | 45 |
| oracle | unprompted_push | core | 20 | 597 | 150 | 67 | 38 |
| pull | unprompted_push | core | 20 | 1225 | 307 | 137 | 77 |
| push_breadcrumb | unprompted_push | core | 20 | 715 | 179 | 80 | 45 |
| push_pages_upper_bound | unprompted_push | core | 20 | 715 | 179 | 80 | 45 |
| push_sections_pull | unprompted_push | core | 20 | 589 | 148 | 66 | 37 |

### `unprompted_push` specifically

- native_grep vs push_breadcrumb_pull @ core: n_achieved=18, required n = {0.05: 'n/a', 0.1: 'n/a', 0.15: 'n/a', 0.2: 'n/a'}
- native_index vs push_breadcrumb_pull @ core: n_achieved=20, required n = {0.05: '778', 0.1: '195', 0.15: '87', 0.2: '49'}
- none vs push_breadcrumb_pull @ core: n_achieved=20, required n = {0.05: '715', 0.1: '179', 0.15: '80', 0.2: '45'}
- oracle vs push_breadcrumb_pull @ core: n_achieved=20, required n = {0.05: '597', 0.1: '150', 0.15: '67', 0.2: '38'}
- pull vs push_breadcrumb_pull @ core: n_achieved=20, required n = {0.05: '1225', 0.1: '307', 0.15: '137', 0.2: '77'}
- push_breadcrumb vs push_breadcrumb_pull @ core: n_achieved=20, required n = {0.05: '715', 0.1: '179', 0.15: '80', 0.2: '45'}
- push_pages_upper_bound vs push_breadcrumb_pull @ core: n_achieved=20, required n = {0.05: '715', 0.1: '179', 0.15: '80', 0.2: '45'}
- push_sections_pull vs push_breadcrumb_pull @ core: n_achieved=20, required n = {0.05: '589', 0.1: '148', 0.15: '66', 0.2: '37'}

## 4. Oracle counter-example

Pooled correctness per arm (n-weighted over every gradable row):

| arm | n | correct | rate | harness failures |
|---|---|---|---|---|
| native_grep | 85 | 61 | 0.718 | 5 |
| native_index | 90 | 62 | 0.689 | 0 |
| none | 90 | 6 | 0.067 | 0 |
| oracle | 90 | 74 | 0.822 | 0 |
| pull | 90 | 67 | 0.744 | 0 |
| push_breadcrumb | 90 | 6 | 0.067 | 0 |
| push_breadcrumb_pull | 89 | 59 | 0.663 | 1 |
| push_pages_upper_bound | 90 | 43 | 0.478 | 0 |
| push_sections_pull | 90 | 75 | 0.833 | 0 |

Correctness per (arm, probe_class, corpus_scale):

| arm | probe_class | scale | n | correct | rate | harness failures |
|---|---|---|---|---|---|---|
| native_grep | abstention | core | 6 | 6 | 1.000 | 0 |
| native_grep | disambiguation | core | 7 | 4 | 0.571 | 0 |
| native_grep | distractor_robustness | core | 4 | 3 | 0.750 | 0 |
| native_grep | follow_through | core | 12 | 11 | 0.917 | 0 |
| native_grep | multi_hop | core | 3 | 0 | 0.000 | 3 |
| native_grep | redundancy | core | 8 | 4 | 0.500 | 0 |
| native_grep | single_hop | core | 15 | 15 | 1.000 | 0 |
| native_grep | temporal | core | 12 | 12 | 1.000 | 0 |
| native_grep | unprompted_push | core | 18 | 6 | 0.333 | 2 |
| native_index | abstention | core | 6 | 2 | 0.333 | 0 |
| native_index | disambiguation | core | 7 | 7 | 1.000 | 0 |
| native_index | distractor_robustness | core | 4 | 3 | 0.750 | 0 |
| native_index | follow_through | core | 12 | 12 | 1.000 | 0 |
| native_index | multi_hop | core | 6 | 2 | 0.333 | 0 |
| native_index | redundancy | core | 8 | 6 | 0.750 | 0 |
| native_index | single_hop | core | 15 | 12 | 0.800 | 0 |
| native_index | temporal | core | 12 | 10 | 0.833 | 0 |
| native_index | unprompted_push | core | 20 | 8 | 0.400 | 0 |
| none | abstention | core | 6 | 6 | 1.000 | 0 |
| none | disambiguation | core | 7 | 0 | 0.000 | 0 |
| none | distractor_robustness | core | 4 | 0 | 0.000 | 0 |
| none | follow_through | core | 12 | 0 | 0.000 | 0 |
| none | multi_hop | core | 6 | 0 | 0.000 | 0 |
| none | redundancy | core | 8 | 0 | 0.000 | 0 |
| none | single_hop | core | 15 | 0 | 0.000 | 0 |
| none | temporal | core | 12 | 0 | 0.000 | 0 |
| none | unprompted_push | core | 20 | 0 | 0.000 | 0 |
| oracle | abstention | core | 6 | 6 | 1.000 | 0 |
| oracle | disambiguation | core | 7 | 7 | 1.000 | 0 |
| oracle | distractor_robustness | core | 4 | 4 | 1.000 | 0 |
| oracle | follow_through | core | 12 | 11 | 0.917 | 0 |
| oracle | multi_hop | core | 6 | 5 | 0.833 | 0 |
| oracle | redundancy | core | 8 | 6 | 0.750 | 0 |
| oracle | single_hop | core | 15 | 14 | 0.933 | 0 |
| oracle | temporal | core | 12 | 12 | 1.000 | 0 |
| oracle | unprompted_push | core | 20 | 9 | 0.450 | 0 |
| pull | abstention | core | 6 | 6 | 1.000 | 0 |
| pull | disambiguation | core | 7 | 7 | 1.000 | 0 |
| pull | distractor_robustness | core | 4 | 4 | 1.000 | 0 |
| pull | follow_through | core | 12 | 12 | 1.000 | 0 |
| pull | multi_hop | core | 6 | 2 | 0.333 | 0 |
| pull | redundancy | core | 8 | 5 | 0.625 | 0 |
| pull | single_hop | core | 15 | 15 | 1.000 | 0 |
| pull | temporal | core | 12 | 11 | 0.917 | 0 |
| pull | unprompted_push | core | 20 | 5 | 0.250 | 0 |
| push_breadcrumb | abstention | core | 6 | 6 | 1.000 | 0 |
| push_breadcrumb | disambiguation | core | 7 | 0 | 0.000 | 0 |
| push_breadcrumb | distractor_robustness | core | 4 | 0 | 0.000 | 0 |
| push_breadcrumb | follow_through | core | 12 | 0 | 0.000 | 0 |
| push_breadcrumb | multi_hop | core | 6 | 0 | 0.000 | 0 |
| push_breadcrumb | redundancy | core | 8 | 0 | 0.000 | 0 |
| push_breadcrumb | single_hop | core | 15 | 0 | 0.000 | 0 |
| push_breadcrumb | temporal | core | 12 | 0 | 0.000 | 0 |
| push_breadcrumb | unprompted_push | core | 20 | 0 | 0.000 | 0 |
| push_breadcrumb_pull | abstention | core | 6 | 3 | 0.500 | 0 |
| push_breadcrumb_pull | disambiguation | core | 7 | 7 | 1.000 | 0 |
| push_breadcrumb_pull | distractor_robustness | core | 4 | 3 | 0.750 | 0 |
| push_breadcrumb_pull | follow_through | core | 12 | 9 | 0.750 | 0 |
| push_breadcrumb_pull | multi_hop | core | 5 | 1 | 0.200 | 1 |
| push_breadcrumb_pull | redundancy | core | 8 | 4 | 0.500 | 0 |
| push_breadcrumb_pull | single_hop | core | 15 | 15 | 1.000 | 0 |
| push_breadcrumb_pull | temporal | core | 12 | 10 | 0.833 | 0 |
| push_breadcrumb_pull | unprompted_push | core | 20 | 7 | 0.350 | 0 |
| push_pages_upper_bound | abstention | core | 6 | 6 | 1.000 | 0 |
| push_pages_upper_bound | disambiguation | core | 7 | 7 | 1.000 | 0 |
| push_pages_upper_bound | distractor_robustness | core | 4 | 4 | 1.000 | 0 |
| push_pages_upper_bound | follow_through | core | 12 | 0 | 0.000 | 0 |
| push_pages_upper_bound | multi_hop | core | 6 | 0 | 0.000 | 0 |
| push_pages_upper_bound | redundancy | core | 8 | 6 | 0.750 | 0 |
| push_pages_upper_bound | single_hop | core | 15 | 8 | 0.533 | 0 |
| push_pages_upper_bound | temporal | core | 12 | 12 | 1.000 | 0 |
| push_pages_upper_bound | unprompted_push | core | 20 | 0 | 0.000 | 0 |
| push_sections_pull | abstention | core | 6 | 5 | 0.833 | 0 |
| push_sections_pull | disambiguation | core | 7 | 7 | 1.000 | 0 |
| push_sections_pull | distractor_robustness | core | 4 | 4 | 1.000 | 0 |
| push_sections_pull | follow_through | core | 12 | 12 | 1.000 | 0 |
| push_sections_pull | multi_hop | core | 6 | 2 | 0.333 | 0 |
| push_sections_pull | redundancy | core | 8 | 6 | 0.750 | 0 |
| push_sections_pull | single_hop | core | 15 | 15 | 1.000 | 0 |
| push_sections_pull | temporal | core | 12 | 12 | 1.000 | 0 |
| push_sections_pull | unprompted_push | core | 20 | 12 | 0.600 | 0 |

Every `oracle` probe graded incorrect (16 probes):

| probe_class | scale | replicate | probe_id | push_breadcrumb_pull correct |
|---|---|---|---|---|
| follow_through | core | 1 | gilcrest_vendor_decision | False |
| multi_hop | core | 1 | portal_design_reviewer | False |
| redundancy | core | 0 | keelbridge_programme_scope | False |
| redundancy | core | 1 | keelbridge_programme_scope | False |
| single_hop | core | 1 | bramfield_retainer_renewal | True |
| unprompted_push | core | 0 | cache_layer_merge | True |
| unprompted_push | core | 0 | connection_pool_widen | False |
| unprompted_push | core | 1 | connection_pool_widen | False |
| unprompted_push | core | 0 | export_flag_deletion | False |
| unprompted_push | core | 1 | export_flag_deletion | False |
| unprompted_push | core | 0 | invoice_role_merge | False |
| unprompted_push | core | 0 | rate_limit_removal | False |
| unprompted_push | core | 1 | rate_limit_removal | False |
| unprompted_push | core | 0 | tls_library_bump | False |
| unprompted_push | core | 0 | webhook_backoff_lower | False |
| unprompted_push | core | 1 | webhook_backoff_lower | False |

## 5. Positive control: `oracle` on `unprompted_push`

| scale | replicate | n | correct | rate | harness failures |
|---|---|---|---|---|---|
| core | 0 | 10 | 3 | 0.300 | 0 |
| core | 1 | 10 | 6 | 0.600 | 0 |

## 6. Grid coverage

47 probes have at least one row; 43 are complete across every (scale, replicate, arm) this store carries. 10 probes have no row at all.

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
| `invoiceref_rename` |

Partially covered:

| probe_id | class | cells | missing (scale/replicate/arm) |
|---|---|---|---|
| `bramfield_retainer_renewal` | single_hop | 9/18 | core/0/native_grep, core/0/native_index, core/0/none, core/0/oracle, core/0/pull, core/0/push_breadcrumb, +3 more |
| `cache_layer_merge` | unprompted_push | 9/18 | core/1/native_grep, core/1/native_index, core/1/none, core/1/oracle, core/1/pull, core/1/push_breadcrumb, +3 more |
| `relay_sync_fix` | unprompted_push | 9/18 | core/0/native_grep, core/0/native_index, core/0/none, core/0/oracle, core/0/pull, core/0/push_breadcrumb, +3 more |
| `surname_is_ambiguous` | disambiguation | 9/18 | core/1/native_grep, core/1/native_index, core/1/none, core/1/oracle, core/1/pull, core/1/push_breadcrumb, +3 more |

## 7. Actual spend for this run

Priced at `claude-sonnet-5` rates with the cache multipliers `athenaeum.models` documents (1.25x on a cache write, 0.1x on a cache read), over EVERY persisted row -- a cell that tripped the turn cap was paid for exactly like one that answered.

| rows counted | input | cache write | cache read | output | billable input | USD |
|---|---|---|---|---|---|---|
| 810 | 6,827,682 | 0 | 0 | 327,765 | 6,827,682 | $25.40 |

Total tokens: 7,155,447.
