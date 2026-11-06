<!-- SPDX-License-Identifier: Apache-2.0 -->

<!--
Noise-floor summary over the POOLED store of runs 42169666857 + 42174214012 (1824 cells after de-duplication), rendered by `tests/evals/noise_floor.py` inside Actions, where the artifact is reachable. This is the derived measurement `north-star-noise-floor-2026-11-06.md` cites.
-->

# North-star grid noise floor (athenaeum#2309)

- run_id: 42169666857
- generated: 2026-11-06T06:57:42Z
- model: claude-haiku-4-5-20251001
  - model NOT independently verified for arms: native_grep, native_index, none, oracle, pull, push_breadcrumb, push_breadcrumb_pull, push_pages_upper_bound (pre-athenaeum#2309 store rows with no RolloutRecord.model and no cli-mode transcript to fall back to)
- search_backend: vector
- grader_revision: athenaeum#1935 (code computing THIS report, not the rows)
- git_sha: f925ab8515d1 (code computing THIS report, not the rows)
- athenaeum_version: 0.23.4
- corpus_digest[core]: 3c5ea876688b089e
- corpus_digest[medium]: 42e6ce0dfdc53e02
- rows: 1824 total, 1764 gradable, 60 harness failures, 0 torn, 16 duplicate

## 1. Replicate-to-replicate variance

| probe_class | scale | arm | n (rep0/rep1) | rate rep0 | rate rep1 | n paired | discordance | SD(class rate) | df | estimator |
|---|---|---|---|---|---|---|---|---|---|---|
| abstention | core | native_grep | 2/1 | 1.000 | 0.000 | 1 | 1.000 | 0.000 | 0 | paired discordance |
| abstention | core | native_index | 3/3 | 0.667 | 1.000 | 3 | 0.333 | 0.192 | 2 | paired discordance |
| abstention | core | none | 3/3 | 1.000 | 1.000 | 3 | 0.000 | 0.000 | 2 | paired discordance |
| abstention | core | oracle | 3/3 | 1.000 | 1.000 | 3 | 0.000 | 0.000 | 2 | paired discordance |
| abstention | core | pull | 2/2 | 1.000 | 1.000 | 2 | 0.000 | 0.000 | 1 | paired discordance |
| abstention | core | push_breadcrumb | 3/3 | 1.000 | 1.000 | 3 | 0.000 | 0.000 | 2 | paired discordance |
| abstention | core | push_breadcrumb_pull | 3/3 | 0.667 | 1.000 | 3 | 0.333 | 0.192 | 2 | paired discordance |
| abstention | core | push_pages_upper_bound | 3/3 | 1.000 | 1.000 | 3 | 0.000 | 0.000 | 2 | paired discordance |
| abstention | medium | native_grep | 2/2 | 1.000 | 1.000 | 2 | 0.000 | 0.000 | 1 | paired discordance |
| abstention | medium | native_index | 3/3 | 1.000 | 0.667 | 3 | 0.333 | 0.192 | 2 | paired discordance |
| abstention | medium | none | 3/3 | 1.000 | 1.000 | 3 | 0.000 | 0.000 | 2 | paired discordance |
| abstention | medium | oracle | 3/3 | 1.000 | 1.000 | 3 | 0.000 | 0.000 | 2 | paired discordance |
| abstention | medium | pull | 2/2 | 1.000 | 1.000 | 2 | 0.000 | 0.000 | 1 | paired discordance |
| abstention | medium | push_breadcrumb | 3/3 | 0.333 | 0.667 | 3 | 0.333 | 0.192 | 2 | paired discordance |
| abstention | medium | push_breadcrumb_pull | 3/2 | 1.000 | 1.000 | 2 | 0.000 | 0.000 | 1 | paired discordance |
| abstention | medium | push_pages_upper_bound | 3/3 | 1.000 | 1.000 | 3 | 0.000 | 0.000 | 2 | paired discordance |
| aggregation | core | native_grep | 2/3 | 1.000 | 1.000 | 2 | 0.000 | 0.000 | 1 | paired discordance |
| aggregation | core | native_index | 3/3 | 0.000 | 0.000 | 3 | 0.000 | 0.000 | 2 | paired discordance |
| aggregation | core | none | 3/3 | 0.000 | 0.000 | 3 | 0.000 | 0.000 | 2 | paired discordance |
| aggregation | core | oracle | 3/3 | 0.667 | 0.667 | 3 | 0.000 | 0.000 | 2 | paired discordance |
| aggregation | core | pull | 3/3 | 0.000 | 0.333 | 3 | 0.333 | 0.192 | 2 | paired discordance |
| aggregation | core | push_breadcrumb | 3/3 | 0.000 | 0.000 | 3 | 0.000 | 0.000 | 2 | paired discordance |
| aggregation | core | push_breadcrumb_pull | 3/3 | 0.333 | 0.333 | 3 | 0.667 | 0.333 | 2 | paired discordance |
| aggregation | core | push_pages_upper_bound | 3/3 | 0.000 | 0.000 | 3 | 0.000 | 0.000 | 2 | paired discordance |
| aggregation | medium | native_grep | 1/1 | 1.000 | 1.000 | 1 | 0.000 | 0.000 | 0 | paired discordance |
| aggregation | medium | native_index | 3/3 | 0.333 | 0.333 | 3 | 0.000 | 0.000 | 2 | paired discordance |
| aggregation | medium | none | 3/3 | 0.000 | 0.000 | 3 | 0.000 | 0.000 | 2 | paired discordance |
| aggregation | medium | oracle | 3/3 | 0.667 | 0.333 | 3 | 0.333 | 0.192 | 2 | paired discordance |
| aggregation | medium | pull | 3/2 | 0.000 | 0.000 | 2 | 0.000 | 0.000 | 1 | paired discordance |
| aggregation | medium | push_breadcrumb | 3/3 | 0.000 | 0.000 | 3 | 0.000 | 0.000 | 2 | paired discordance |
| aggregation | medium | push_breadcrumb_pull | 3/3 | 0.000 | 0.333 | 3 | 0.333 | 0.192 | 2 | paired discordance |
| aggregation | medium | push_pages_upper_bound | 3/3 | 0.000 | 0.000 | 3 | 0.000 | 0.000 | 2 | paired discordance |
| contradiction | core | native_grep | 3/3 | 0.667 | 0.667 | 3 | 0.000 | 0.000 | 2 | paired discordance |
| contradiction | core | native_index | 2/2 | 1.000 | 1.000 | 2 | 0.000 | 0.000 | 1 | paired discordance |
| contradiction | core | none | 3/3 | 0.000 | 0.000 | 3 | 0.000 | 0.000 | 2 | paired discordance |
| contradiction | core | oracle | 3/3 | 1.000 | 1.000 | 3 | 0.000 | 0.000 | 2 | paired discordance |
| contradiction | core | pull | 3/3 | 1.000 | 1.000 | 3 | 0.000 | 0.000 | 2 | paired discordance |
| contradiction | core | push_breadcrumb | 3/3 | 0.000 | 0.000 | 3 | 0.000 | 0.000 | 2 | paired discordance |
| contradiction | core | push_breadcrumb_pull | 3/3 | 1.000 | 1.000 | 3 | 0.000 | 0.000 | 2 | paired discordance |
| contradiction | core | push_pages_upper_bound | 3/3 | 0.333 | 0.333 | 3 | 0.000 | 0.000 | 2 | paired discordance |
| contradiction | medium | native_grep | 3/3 | 1.000 | 0.667 | 3 | 0.333 | 0.192 | 2 | paired discordance |
| contradiction | medium | native_index | 3/3 | 1.000 | 0.667 | 3 | 0.333 | 0.192 | 2 | paired discordance |
| contradiction | medium | none | 3/3 | 0.000 | 0.000 | 3 | 0.000 | 0.000 | 2 | paired discordance |
| contradiction | medium | oracle | 3/3 | 1.000 | 1.000 | 3 | 0.000 | 0.000 | 2 | paired discordance |
| contradiction | medium | pull | 3/3 | 1.000 | 1.000 | 3 | 0.000 | 0.000 | 2 | paired discordance |
| contradiction | medium | push_breadcrumb | 3/3 | 0.000 | 0.000 | 3 | 0.000 | 0.000 | 2 | paired discordance |
| contradiction | medium | push_breadcrumb_pull | 3/3 | 1.000 | 1.000 | 3 | 0.000 | 0.000 | 2 | paired discordance |
| contradiction | medium | push_pages_upper_bound | 3/3 | 0.333 | 0.333 | 3 | 0.000 | 0.000 | 2 | paired discordance |
| disambiguation | core | native_grep | 4/4 | 0.750 | 0.500 | 4 | 0.750 | 0.293 | 3 | paired discordance |
| disambiguation | core | native_index | 4/4 | 0.500 | 0.750 | 4 | 0.250 | 0.153 | 3 | paired discordance |
| disambiguation | core | none | 4/4 | 0.000 | 0.000 | 4 | 0.000 | 0.000 | 3 | paired discordance |
| disambiguation | core | oracle | 4/4 | 1.000 | 1.000 | 4 | 0.000 | 0.000 | 3 | paired discordance |
| disambiguation | core | pull | 4/4 | 1.000 | 1.000 | 4 | 0.000 | 0.000 | 3 | paired discordance |
| disambiguation | core | push_breadcrumb | 4/4 | 0.000 | 0.000 | 4 | 0.000 | 0.000 | 3 | paired discordance |
| disambiguation | core | push_breadcrumb_pull | 4/4 | 1.000 | 1.000 | 4 | 0.000 | 0.000 | 3 | paired discordance |
| disambiguation | core | push_pages_upper_bound | 4/4 | 1.000 | 1.000 | 4 | 0.000 | 0.000 | 3 | paired discordance |
| disambiguation | medium | native_grep | 4/4 | 0.750 | 0.500 | 4 | 0.250 | 0.153 | 3 | paired discordance |
| disambiguation | medium | native_index | 4/4 | 0.250 | 0.250 | 4 | 0.000 | 0.000 | 3 | paired discordance |
| disambiguation | medium | none | 4/4 | 0.000 | 0.000 | 4 | 0.000 | 0.000 | 3 | paired discordance |
| disambiguation | medium | oracle | 4/4 | 1.000 | 1.000 | 4 | 0.000 | 0.000 | 3 | paired discordance |
| disambiguation | medium | pull | 4/4 | 1.000 | 1.000 | 4 | 0.000 | 0.000 | 3 | paired discordance |
| disambiguation | medium | push_breadcrumb | 4/4 | 0.000 | 0.000 | 4 | 0.000 | 0.000 | 3 | paired discordance |
| disambiguation | medium | push_breadcrumb_pull | 4/4 | 1.000 | 1.000 | 4 | 0.000 | 0.000 | 3 | paired discordance |
| disambiguation | medium | push_pages_upper_bound | 4/4 | 1.000 | 1.000 | 4 | 0.000 | 0.000 | 3 | paired discordance |
| distractor_robustness | core | native_grep | 2/2 | 0.000 | 0.500 | 2 | 0.500 | 0.250 | 1 | paired discordance |
| distractor_robustness | core | native_index | 2/2 | 0.000 | 0.000 | 2 | 0.000 | 0.000 | 1 | paired discordance |
| distractor_robustness | core | none | 2/2 | 0.000 | 0.000 | 2 | 0.000 | 0.000 | 1 | paired discordance |
| distractor_robustness | core | oracle | 2/2 | 1.000 | 1.000 | 2 | 0.000 | 0.000 | 1 | paired discordance |
| distractor_robustness | core | pull | 2/2 | 1.000 | 0.500 | 2 | 0.500 | 0.250 | 1 | paired discordance |
| distractor_robustness | core | push_breadcrumb | 2/2 | 0.000 | 0.000 | 2 | 0.000 | 0.000 | 1 | paired discordance |
| distractor_robustness | core | push_breadcrumb_pull | 2/2 | 1.000 | 1.000 | 2 | 0.000 | 0.000 | 1 | paired discordance |
| distractor_robustness | core | push_pages_upper_bound | 2/2 | 1.000 | 1.000 | 2 | 0.000 | 0.000 | 1 | paired discordance |
| distractor_robustness | medium | native_grep | 2/2 | 0.500 | 0.500 | 2 | 0.000 | 0.000 | 1 | paired discordance |
| distractor_robustness | medium | native_index | 2/2 | 0.000 | 0.500 | 2 | 0.500 | 0.250 | 1 | paired discordance |
| distractor_robustness | medium | none | 2/2 | 0.000 | 0.000 | 2 | 0.000 | 0.000 | 1 | paired discordance |
| distractor_robustness | medium | oracle | 2/2 | 1.000 | 1.000 | 2 | 0.000 | 0.000 | 1 | paired discordance |
| distractor_robustness | medium | pull | 2/2 | 0.500 | 0.500 | 2 | 0.000 | 0.000 | 1 | paired discordance |
| distractor_robustness | medium | push_breadcrumb | 2/2 | 0.000 | 0.000 | 2 | 0.000 | 0.000 | 1 | paired discordance |
| distractor_robustness | medium | push_breadcrumb_pull | 2/2 | 1.000 | 1.000 | 2 | 0.000 | 0.000 | 1 | paired discordance |
| distractor_robustness | medium | push_pages_upper_bound | 2/2 | 0.500 | 0.500 | 2 | 0.000 | 0.000 | 1 | paired discordance |
| follow_through | core | native_grep | 5/6 | 0.800 | 0.833 | 5 | 0.000 | 0.000 | 4 | paired discordance |
| follow_through | core | native_index | 5/6 | 0.800 | 1.000 | 5 | 0.200 | 0.126 | 4 | paired discordance |
| follow_through | core | none | 6/6 | 0.000 | 0.000 | 6 | 0.000 | 0.000 | 5 | paired discordance |
| follow_through | core | oracle | 6/6 | 1.000 | 1.000 | 6 | 0.000 | 0.000 | 5 | paired discordance |
| follow_through | core | pull | 6/6 | 1.000 | 1.000 | 6 | 0.000 | 0.000 | 5 | paired discordance |
| follow_through | core | push_breadcrumb | 6/6 | 0.000 | 0.000 | 6 | 0.000 | 0.000 | 5 | paired discordance |
| follow_through | core | push_breadcrumb_pull | 6/5 | 0.833 | 1.000 | 5 | 0.000 | 0.000 | 4 | paired discordance |
| follow_through | core | push_pages_upper_bound | 6/6 | 0.000 | 0.000 | 6 | 0.000 | 0.000 | 5 | paired discordance |
| follow_through | medium | native_grep | 3/2 | 1.000 | 1.000 | 2 | 0.000 | 0.000 | 1 | paired discordance |
| follow_through | medium | native_index | 6/6 | 1.000 | 1.000 | 6 | 0.000 | 0.000 | 5 | paired discordance |
| follow_through | medium | none | 6/6 | 0.000 | 0.000 | 6 | 0.000 | 0.000 | 5 | paired discordance |
| follow_through | medium | oracle | 6/6 | 1.000 | 1.000 | 6 | 0.000 | 0.000 | 5 | paired discordance |
| follow_through | medium | pull | 6/5 | 1.000 | 1.000 | 5 | 0.000 | 0.000 | 4 | paired discordance |
| follow_through | medium | push_breadcrumb | 6/6 | 0.000 | 0.000 | 6 | 0.000 | 0.000 | 5 | paired discordance |
| follow_through | medium | push_breadcrumb_pull | 6/6 | 1.000 | 0.833 | 6 | 0.167 | 0.108 | 5 | paired discordance |
| follow_through | medium | push_pages_upper_bound | 6/6 | 0.000 | 0.000 | 6 | 0.000 | 0.000 | 5 | paired discordance |
| multi_hop | core | native_grep | 3/2 | 1.000 | 1.000 | 2 | 0.000 | 0.000 | 1 | paired discordance |
| multi_hop | core | native_index | 3/3 | 0.667 | 0.667 | 3 | 0.000 | 0.000 | 2 | paired discordance |
| multi_hop | core | none | 3/3 | 0.000 | 0.000 | 3 | 0.000 | 0.000 | 2 | paired discordance |
| multi_hop | core | oracle | 3/3 | 0.667 | 1.000 | 3 | 0.333 | 0.192 | 2 | paired discordance |
| multi_hop | core | pull | 2/2 | 1.000 | 1.000 | 2 | 0.000 | 0.000 | 1 | paired discordance |
| multi_hop | core | push_breadcrumb | 3/3 | 0.000 | 0.000 | 3 | 0.000 | 0.000 | 2 | paired discordance |
| multi_hop | core | push_breadcrumb_pull | 2/3 | 1.000 | 0.667 | 2 | 0.000 | 0.000 | 1 | paired discordance |
| multi_hop | core | push_pages_upper_bound | 3/3 | 0.000 | 0.000 | 3 | 0.000 | 0.000 | 2 | paired discordance |
| multi_hop | medium | native_grep | 1/2 | 1.000 | 0.500 | 1 | 0.000 | 0.000 | 0 | paired discordance |
| multi_hop | medium | native_index | 3/3 | 0.333 | 0.667 | 3 | 0.333 | 0.192 | 2 | paired discordance |
| multi_hop | medium | none | 3/3 | 0.000 | 0.000 | 3 | 0.000 | 0.000 | 2 | paired discordance |
| multi_hop | medium | oracle | 3/3 | 1.000 | 0.667 | 3 | 0.333 | 0.192 | 2 | paired discordance |
| multi_hop | medium | pull | 2/1 | 0.500 | 1.000 | 1 | 0.000 | 0.000 | 0 | paired discordance |
| multi_hop | medium | push_breadcrumb | 3/3 | 0.000 | 0.000 | 3 | 0.000 | 0.000 | 2 | paired discordance |
| multi_hop | medium | push_breadcrumb_pull | 3/3 | 0.333 | 0.667 | 3 | 0.333 | 0.192 | 2 | paired discordance |
| multi_hop | medium | push_pages_upper_bound | 3/3 | 0.000 | 0.000 | 3 | 0.000 | 0.000 | 2 | paired discordance |
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
| redundancy | core | native_grep | 4/4 | 0.250 | 0.250 | 4 | 0.500 | 0.250 | 3 | paired discordance |
| redundancy | core | native_index | 4/4 | 0.250 | 0.000 | 4 | 0.250 | 0.153 | 3 | paired discordance |
| redundancy | core | none | 4/4 | 0.000 | 0.000 | 4 | 0.000 | 0.000 | 3 | paired discordance |
| redundancy | core | oracle | 4/4 | 0.500 | 0.500 | 4 | 0.000 | 0.000 | 3 | paired discordance |
| redundancy | core | pull | 4/4 | 0.750 | 1.000 | 4 | 0.250 | 0.153 | 3 | paired discordance |
| redundancy | core | push_breadcrumb | 4/4 | 0.000 | 0.000 | 4 | 0.000 | 0.000 | 3 | paired discordance |
| redundancy | core | push_breadcrumb_pull | 4/4 | 0.750 | 0.750 | 4 | 0.000 | 0.000 | 3 | paired discordance |
| redundancy | core | push_pages_upper_bound | 4/4 | 0.750 | 0.750 | 4 | 0.000 | 0.000 | 3 | paired discordance |
| redundancy | medium | native_grep | 4/4 | 0.000 | 0.000 | 4 | 0.000 | 0.000 | 3 | paired discordance |
| redundancy | medium | native_index | 4/4 | 0.000 | 0.250 | 4 | 0.250 | 0.153 | 3 | paired discordance |
| redundancy | medium | none | 4/4 | 0.000 | 0.000 | 4 | 0.000 | 0.000 | 3 | paired discordance |
| redundancy | medium | oracle | 4/4 | 0.750 | 0.500 | 4 | 0.250 | 0.153 | 3 | paired discordance |
| redundancy | medium | pull | 4/4 | 0.750 | 0.750 | 4 | 0.000 | 0.000 | 3 | paired discordance |
| redundancy | medium | push_breadcrumb | 4/4 | 0.000 | 0.000 | 4 | 0.000 | 0.000 | 3 | paired discordance |
| redundancy | medium | push_breadcrumb_pull | 4/4 | 1.000 | 0.750 | 4 | 0.250 | 0.153 | 3 | paired discordance |
| redundancy | medium | push_pages_upper_bound | 4/4 | 0.750 | 1.000 | 4 | 0.250 | 0.153 | 3 | paired discordance |
| single_hop | core | native_grep | 8/8 | 1.000 | 0.875 | 8 | 0.125 | 0.083 | 7 | paired discordance |
| single_hop | core | native_index | 8/8 | 0.875 | 0.750 | 8 | 0.375 | 0.150 | 7 | paired discordance |
| single_hop | core | none | 8/8 | 0.000 | 0.000 | 8 | 0.000 | 0.000 | 7 | paired discordance |
| single_hop | core | oracle | 8/8 | 0.750 | 0.625 | 8 | 0.375 | 0.150 | 7 | paired discordance |
| single_hop | core | pull | 8/8 | 0.875 | 0.875 | 8 | 0.000 | 0.000 | 7 | paired discordance |
| single_hop | core | push_breadcrumb | 8/8 | 0.000 | 0.000 | 8 | 0.000 | 0.000 | 7 | paired discordance |
| single_hop | core | push_breadcrumb_pull | 8/8 | 0.875 | 0.750 | 8 | 0.125 | 0.083 | 7 | paired discordance |
| single_hop | core | push_pages_upper_bound | 8/8 | 0.375 | 0.375 | 8 | 0.000 | 0.000 | 7 | paired discordance |
| single_hop | medium | native_grep | 8/8 | 0.750 | 1.000 | 8 | 0.250 | 0.108 | 7 | paired discordance |
| single_hop | medium | native_index | 8/8 | 0.750 | 1.000 | 8 | 0.250 | 0.108 | 7 | paired discordance |
| single_hop | medium | none | 8/8 | 0.000 | 0.000 | 8 | 0.000 | 0.000 | 7 | paired discordance |
| single_hop | medium | oracle | 8/8 | 0.750 | 0.750 | 8 | 0.000 | 0.000 | 7 | paired discordance |
| single_hop | medium | pull | 8/8 | 0.875 | 1.000 | 8 | 0.125 | 0.083 | 7 | paired discordance |
| single_hop | medium | push_breadcrumb | 8/8 | 0.000 | 0.000 | 8 | 0.000 | 0.000 | 7 | paired discordance |
| single_hop | medium | push_breadcrumb_pull | 8/8 | 1.000 | 1.000 | 8 | 0.000 | 0.000 | 7 | paired discordance |
| single_hop | medium | push_pages_upper_bound | 8/8 | 0.375 | 0.375 | 8 | 0.000 | 0.000 | 7 | paired discordance |
| temporal | core | native_grep | 6/6 | 1.000 | 1.000 | 6 | 0.000 | 0.000 | 5 | paired discordance |
| temporal | core | native_index | 6/6 | 0.667 | 0.500 | 6 | 0.167 | 0.108 | 5 | paired discordance |
| temporal | core | none | 6/6 | 0.000 | 0.000 | 6 | 0.000 | 0.000 | 5 | paired discordance |
| temporal | core | oracle | 6/6 | 1.000 | 1.000 | 6 | 0.000 | 0.000 | 5 | paired discordance |
| temporal | core | pull | 6/6 | 1.000 | 0.833 | 6 | 0.167 | 0.108 | 5 | paired discordance |
| temporal | core | push_breadcrumb | 6/6 | 0.000 | 0.000 | 6 | 0.000 | 0.000 | 5 | paired discordance |
| temporal | core | push_breadcrumb_pull | 6/6 | 1.000 | 1.000 | 6 | 0.000 | 0.000 | 5 | paired discordance |
| temporal | core | push_pages_upper_bound | 6/6 | 1.000 | 1.000 | 6 | 0.000 | 0.000 | 5 | paired discordance |
| temporal | medium | native_grep | 5/6 | 0.600 | 0.833 | 5 | 0.200 | 0.126 | 4 | paired discordance |
| temporal | medium | native_index | 6/6 | 0.833 | 0.500 | 6 | 0.333 | 0.136 | 5 | paired discordance |
| temporal | medium | none | 6/6 | 0.000 | 0.000 | 6 | 0.000 | 0.000 | 5 | paired discordance |
| temporal | medium | oracle | 6/6 | 1.000 | 1.000 | 6 | 0.000 | 0.000 | 5 | paired discordance |
| temporal | medium | pull | 6/6 | 1.000 | 1.000 | 6 | 0.000 | 0.000 | 5 | paired discordance |
| temporal | medium | push_breadcrumb | 6/6 | 0.000 | 0.000 | 6 | 0.000 | 0.000 | 5 | paired discordance |
| temporal | medium | push_breadcrumb_pull | 6/6 | 1.000 | 1.000 | 6 | 0.000 | 0.000 | 5 | paired discordance |
| temporal | medium | push_pages_upper_bound | 6/6 | 1.000 | 1.000 | 6 | 0.000 | 0.000 | 5 | paired discordance |
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
| native_grep | abstention | core | 3 | 0.667 | 0.667 | 1.321 (insufficient n for normal approx -- use exact test) | 1.078 | False |
| native_index | abstention | core | 6 | 0.833 | 0.833 | 0.660 (insufficient n for normal approx -- use exact test) | 0.603 | False |
| none | abstention | core | 6 | 1.000 | 0.833 | 0.426 (insufficient n for normal approx -- use exact test) | 0.447 | False |
| oracle | abstention | core | 6 | 1.000 | 0.833 | 0.426 (insufficient n for normal approx -- use exact test) | 0.447 | False |
| pull | abstention | core | 4 | 1.000 | 0.750 | 0.607 (insufficient n for normal approx -- use exact test) | 0.655 | False |
| push_breadcrumb | abstention | core | 6 | 1.000 | 0.833 | 0.426 (insufficient n for normal approx -- use exact test) | 0.447 | False |
| push_pages_upper_bound | abstention | core | 6 | 1.000 | 0.833 | 0.426 (insufficient n for normal approx -- use exact test) | 0.447 | False |
| native_grep | abstention | medium | 4 | 1.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.000 | False |
| native_index | abstention | medium | 5 | 0.800 | 1.000 | 0.501 (insufficient n for normal approx -- use exact test) | 0.532 | False |
| none | abstention | medium | 5 | 1.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.000 | False |
| oracle | abstention | medium | 5 | 1.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.000 | False |
| pull | abstention | medium | 4 | 1.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.000 | False |
| push_breadcrumb | abstention | medium | 5 | 0.400 | 1.000 | 0.614 (insufficient n for normal approx -- use exact test) | 0.812 | False |
| push_pages_upper_bound | abstention | medium | 5 | 1.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.000 | False |
| native_grep | aggregation | core | 5 | 1.000 | 0.400 | 0.614 (insufficient n for normal approx -- use exact test) | 0.812 | False |
| native_index | aggregation | core | 6 | 0.000 | 0.333 | 0.539 (insufficient n for normal approx -- use exact test) | 0.603 | False |
| none | aggregation | core | 6 | 0.000 | 0.333 | 0.539 (insufficient n for normal approx -- use exact test) | 0.603 | False |
| oracle | aggregation | core | 6 | 0.667 | 0.333 | 0.852 (insufficient n for normal approx -- use exact test) | 0.809 | False |
| pull | aggregation | core | 6 | 0.167 | 0.333 | 0.426 (insufficient n for normal approx -- use exact test) | 0.700 | False |
| push_breadcrumb | aggregation | core | 6 | 0.000 | 0.333 | 0.539 (insufficient n for normal approx -- use exact test) | 0.603 | False |
| push_pages_upper_bound | aggregation | core | 6 | 0.000 | 0.333 | 0.539 (insufficient n for normal approx -- use exact test) | 0.603 | False |
| native_grep | aggregation | medium | 2 | 1.000 | 0.000 | 0.000 (insufficient n for normal approx -- use exact test) | 1.401 | False |
| native_index | aggregation | medium | 6 | 0.333 | 0.167 | 0.786 (insufficient n for normal approx -- use exact test) | 0.700 | False |
| none | aggregation | medium | 6 | 0.000 | 0.167 | 0.426 (insufficient n for normal approx -- use exact test) | 0.447 | False |
| oracle | aggregation | medium | 6 | 0.500 | 0.167 | 0.539 (insufficient n for normal approx -- use exact test) | 0.762 | False |
| pull | aggregation | medium | 5 | 0.000 | 0.200 | 0.501 (insufficient n for normal approx -- use exact test) | 0.532 | False |
| push_breadcrumb | aggregation | medium | 6 | 0.000 | 0.167 | 0.426 (insufficient n for normal approx -- use exact test) | 0.447 | False |
| push_pages_upper_bound | aggregation | medium | 6 | 0.000 | 0.167 | 0.426 (insufficient n for normal approx -- use exact test) | 0.447 | False |
| native_grep | contradiction | core | 6 | 0.667 | 1.000 | 0.539 (insufficient n for normal approx -- use exact test) | 0.603 | False |
| native_index | contradiction | core | 4 | 1.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.000 | False |
| none | contradiction | core | 6 | 0.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.809 | False |
| oracle | contradiction | core | 6 | 1.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.000 | False |
| pull | contradiction | core | 6 | 1.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.000 | False |
| push_breadcrumb | contradiction | core | 6 | 0.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.809 | False |
| push_pages_upper_bound | contradiction | core | 6 | 0.333 | 1.000 | 0.539 (insufficient n for normal approx -- use exact test) | 0.762 | False |
| native_grep | contradiction | medium | 6 | 0.833 | 1.000 | 0.426 (insufficient n for normal approx -- use exact test) | 0.447 | False |
| native_index | contradiction | medium | 6 | 0.833 | 1.000 | 0.426 (insufficient n for normal approx -- use exact test) | 0.447 | False |
| none | contradiction | medium | 6 | 0.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.809 | False |
| oracle | contradiction | medium | 6 | 1.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.000 | False |
| pull | contradiction | medium | 6 | 1.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.000 | False |
| push_breadcrumb | contradiction | medium | 6 | 0.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.809 | False |
| push_pages_upper_bound | contradiction | medium | 6 | 0.333 | 1.000 | 0.539 (insufficient n for normal approx -- use exact test) | 0.762 | False |
| native_grep | disambiguation | core | 8 | 0.625 | 1.000 | 0.480 (insufficient n for normal approx -- use exact test) | 0.547 | False |
| native_index | disambiguation | core | 8 | 0.625 | 1.000 | 0.480 (insufficient n for normal approx -- use exact test) | 0.547 | False |
| none | disambiguation | core | 8 | 0.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.700 | False |
| oracle | disambiguation | core | 8 | 1.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.000 | False |
| pull | disambiguation | core | 8 | 1.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.000 | False |
| push_breadcrumb | disambiguation | core | 8 | 0.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.700 | False |
| push_pages_upper_bound | disambiguation | core | 8 | 1.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.000 | False |
| native_grep | disambiguation | medium | 8 | 0.625 | 1.000 | 0.480 (insufficient n for normal approx -- use exact test) | 0.547 | False |
| native_index | disambiguation | medium | 8 | 0.250 | 1.000 | 0.429 (insufficient n for normal approx -- use exact test) | 0.678 | False |
| none | disambiguation | medium | 8 | 0.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.700 | False |
| oracle | disambiguation | medium | 8 | 1.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.000 | False |
| pull | disambiguation | medium | 8 | 1.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.000 | False |
| push_breadcrumb | disambiguation | medium | 8 | 0.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.700 | False |
| push_pages_upper_bound | disambiguation | medium | 8 | 1.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.000 | False |
| native_grep | distractor_robustness | core | 4 | 0.250 | 1.000 | 0.607 (insufficient n for normal approx -- use exact test) | 0.959 | False |
| native_index | distractor_robustness | core | 4 | 0.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.991 | False |
| none | distractor_robustness | core | 4 | 0.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.991 | False |
| oracle | distractor_robustness | core | 4 | 1.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.000 | False |
| pull | distractor_robustness | core | 4 | 0.750 | 1.000 | 0.607 (insufficient n for normal approx -- use exact test) | 0.655 | False |
| push_breadcrumb | distractor_robustness | core | 4 | 0.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.991 | False |
| push_pages_upper_bound | distractor_robustness | core | 4 | 1.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.000 | False |
| native_grep | distractor_robustness | medium | 4 | 0.500 | 1.000 | 0.700 (insufficient n for normal approx -- use exact test) | 0.858 | False |
| native_index | distractor_robustness | medium | 4 | 0.250 | 1.000 | 0.607 (insufficient n for normal approx -- use exact test) | 0.959 | False |
| none | distractor_robustness | medium | 4 | 0.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.991 | False |
| oracle | distractor_robustness | medium | 4 | 1.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.000 | False |
| pull | distractor_robustness | medium | 4 | 0.500 | 1.000 | 0.700 (insufficient n for normal approx -- use exact test) | 0.858 | False |
| push_breadcrumb | distractor_robustness | medium | 4 | 0.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.991 | False |
| push_pages_upper_bound | distractor_robustness | medium | 4 | 0.500 | 1.000 | 0.700 (insufficient n for normal approx -- use exact test) | 0.858 | False |
| native_grep | follow_through | core | 10 | 0.900 | 0.900 | 0.000 (insufficient n for normal approx -- use exact test) | 0.376 | False |
| native_index | follow_through | core | 10 | 0.900 | 0.900 | 0.000 (insufficient n for normal approx -- use exact test) | 0.376 | False |
| none | follow_through | core | 11 | 0.000 | 0.909 | 0.243 (insufficient n for normal approx -- use exact test) | 0.595 | False |
| oracle | follow_through | core | 11 | 1.000 | 0.909 | 0.243 (insufficient n for normal approx -- use exact test) | 0.249 | False |
| pull | follow_through | core | 11 | 1.000 | 0.909 | 0.243 (insufficient n for normal approx -- use exact test) | 0.249 | False |
| push_breadcrumb | follow_through | core | 11 | 0.000 | 0.909 | 0.243 (insufficient n for normal approx -- use exact test) | 0.595 | False |
| push_pages_upper_bound | follow_through | core | 11 | 0.000 | 0.909 | 0.243 (insufficient n for normal approx -- use exact test) | 0.595 | False |
| native_grep | follow_through | medium | 5 | 1.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.000 | False |
| native_index | follow_through | medium | 12 | 1.000 | 0.917 | 0.224 (insufficient n for normal approx -- use exact test) | 0.229 | False |
| none | follow_through | medium | 12 | 0.000 | 0.917 | 0.224 (insufficient n for normal approx -- use exact test) | 0.570 | False |
| oracle | follow_through | medium | 12 | 1.000 | 0.917 | 0.224 (insufficient n for normal approx -- use exact test) | 0.229 | False |
| pull | follow_through | medium | 11 | 1.000 | 0.909 | 0.243 (insufficient n for normal approx -- use exact test) | 0.249 | False |
| push_breadcrumb | follow_through | medium | 12 | 0.000 | 0.917 | 0.224 (insufficient n for normal approx -- use exact test) | 0.570 | False |
| push_pages_upper_bound | follow_through | medium | 12 | 0.000 | 0.917 | 0.224 (insufficient n for normal approx -- use exact test) | 0.570 | False |
| native_grep | multi_hop | core | 4 | 1.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.000 | False |
| native_index | multi_hop | core | 5 | 0.800 | 0.800 | 0.000 (insufficient n for normal approx -- use exact test) | 0.709 | False |
| none | multi_hop | core | 5 | 0.000 | 0.800 | 0.501 (insufficient n for normal approx -- use exact test) | 0.868 | False |
| oracle | multi_hop | core | 5 | 0.800 | 0.800 | 0.792 (insufficient n for normal approx -- use exact test) | 0.709 | False |
| pull | multi_hop | core | 4 | 1.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.000 | False |
| push_breadcrumb | multi_hop | core | 5 | 0.000 | 0.800 | 0.501 (insufficient n for normal approx -- use exact test) | 0.868 | False |
| push_pages_upper_bound | multi_hop | core | 5 | 0.000 | 0.800 | 0.501 (insufficient n for normal approx -- use exact test) | 0.868 | False |
| native_grep | multi_hop | medium | 3 | 0.667 | 0.667 | 0.000 (insufficient n for normal approx -- use exact test) | 1.078 | False |
| native_index | multi_hop | medium | 6 | 0.500 | 0.500 | 0.000 (insufficient n for normal approx -- use exact test) | 0.809 | False |
| none | multi_hop | medium | 6 | 0.000 | 0.500 | 0.572 (insufficient n for normal approx -- use exact test) | 0.700 | False |
| oracle | multi_hop | medium | 6 | 0.833 | 0.500 | 0.852 (insufficient n for normal approx -- use exact test) | 0.762 | False |
| pull | multi_hop | medium | 3 | 0.667 | 0.667 | 0.000 (insufficient n for normal approx -- use exact test) | 1.078 | False |
| push_breadcrumb | multi_hop | medium | 6 | 0.000 | 0.500 | 0.572 (insufficient n for normal approx -- use exact test) | 0.700 | False |
| push_pages_upper_bound | multi_hop | medium | 6 | 0.000 | 0.500 | 0.572 (insufficient n for normal approx -- use exact test) | 0.700 | False |
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
| native_grep | redundancy | core | 8 | 0.250 | 0.750 | 0.700 (insufficient n for normal approx -- use exact test) | 0.700 | False |
| native_index | redundancy | core | 8 | 0.125 | 0.750 | 0.480 (insufficient n for normal approx -- use exact test) | 0.695 | False |
| none | redundancy | core | 8 | 0.000 | 0.750 | 0.429 (insufficient n for normal approx -- use exact test) | 0.678 | False |
| oracle | redundancy | core | 8 | 0.500 | 0.750 | 0.429 (insufficient n for normal approx -- use exact test) | 0.678 | False |
| pull | redundancy | core | 8 | 0.875 | 0.750 | 0.594 (insufficient n for normal approx -- use exact test) | 0.547 | False |
| push_breadcrumb | redundancy | core | 8 | 0.000 | 0.750 | 0.429 (insufficient n for normal approx -- use exact test) | 0.678 | False |
| push_pages_upper_bound | redundancy | core | 8 | 0.750 | 0.750 | 0.000 (insufficient n for normal approx -- use exact test) | 0.607 | False |
| native_grep | redundancy | medium | 8 | 0.000 | 0.875 | 0.328 (insufficient n for normal approx -- use exact test) | 0.695 | False |
| native_index | redundancy | medium | 8 | 0.125 | 0.875 | 0.429 (insufficient n for normal approx -- use exact test) | 0.700 | False |
| none | redundancy | medium | 8 | 0.000 | 0.875 | 0.328 (insufficient n for normal approx -- use exact test) | 0.695 | False |
| oracle | redundancy | medium | 8 | 0.625 | 0.875 | 0.429 (insufficient n for normal approx -- use exact test) | 0.607 | False |
| pull | redundancy | medium | 8 | 0.750 | 0.875 | 0.328 (insufficient n for normal approx -- use exact test) | 0.547 | False |
| push_breadcrumb | redundancy | medium | 8 | 0.000 | 0.875 | 0.328 (insufficient n for normal approx -- use exact test) | 0.695 | False |
| push_pages_upper_bound | redundancy | medium | 8 | 0.875 | 0.875 | 0.495 (insufficient n for normal approx -- use exact test) | 0.463 | False |
| native_grep | single_hop | core | 16 | 0.938 | 0.812 | 0.232 (insufficient n for normal approx -- use exact test) | 0.328 | False |
| native_index | single_hop | core | 16 | 0.812 | 0.812 | 0.350 (insufficient n for normal approx -- use exact test) | 0.387 | False |
| none | single_hop | core | 16 | 0.000 | 0.812 | 0.273 (insufficient n for normal approx -- use exact test) | 0.486 | False |
| oracle | single_hop | core | 16 | 0.688 | 0.812 | 0.420 | 0.429 | True |
| pull | single_hop | core | 16 | 0.875 | 0.812 | 0.170 (insufficient n for normal approx -- use exact test) | 0.360 | False |
| push_breadcrumb | single_hop | core | 16 | 0.000 | 0.812 | 0.273 (insufficient n for normal approx -- use exact test) | 0.486 | False |
| push_pages_upper_bound | single_hop | core | 16 | 0.375 | 0.812 | 0.347 | 0.486 | True |
| native_grep | single_hop | medium | 16 | 0.875 | 1.000 | 0.232 (insufficient n for normal approx -- use exact test) | 0.240 | False |
| native_index | single_hop | medium | 16 | 0.875 | 1.000 | 0.232 (insufficient n for normal approx -- use exact test) | 0.240 | False |
| none | single_hop | medium | 16 | 0.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.495 | False |
| oracle | single_hop | medium | 16 | 0.750 | 1.000 | 0.303 (insufficient n for normal approx -- use exact test) | 0.328 | False |
| pull | single_hop | medium | 16 | 0.938 | 1.000 | 0.170 (insufficient n for normal approx -- use exact test) | 0.172 | False |
| push_breadcrumb | single_hop | medium | 16 | 0.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.495 | False |
| push_pages_upper_bound | single_hop | medium | 16 | 0.375 | 1.000 | 0.339 | 0.459 | True |
| native_grep | temporal | core | 12 | 1.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.000 | False |
| native_index | temporal | core | 12 | 0.583 | 1.000 | 0.399 | 0.464 | True |
| none | temporal | core | 12 | 0.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.572 | False |
| oracle | temporal | core | 12 | 1.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.000 | False |
| pull | temporal | core | 12 | 0.917 | 1.000 | 0.224 (insufficient n for normal approx -- use exact test) | 0.229 | False |
| push_breadcrumb | temporal | core | 12 | 0.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.572 | False |
| push_pages_upper_bound | temporal | core | 12 | 1.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.000 | False |
| native_grep | temporal | medium | 11 | 0.727 | 1.000 | 0.376 (insufficient n for normal approx -- use exact test) | 0.410 | False |
| native_index | temporal | medium | 12 | 0.667 | 1.000 | 0.381 (insufficient n for normal approx -- use exact test) | 0.426 | False |
| none | temporal | medium | 12 | 0.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.572 | False |
| oracle | temporal | medium | 12 | 1.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.000 | False |
| pull | temporal | medium | 12 | 1.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.000 | False |
| push_breadcrumb | temporal | medium | 12 | 0.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.572 | False |
| push_pages_upper_bound | temporal | medium | 12 | 1.000 | 1.000 | 0.000 (insufficient n for normal approx -- use exact test) | 0.000 | False |
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
| native_grep | abstention | core | 3 | 2094 | 524 | 233 | 131 |
| native_index | abstention | core | 6 | 1047 | 262 | 117 | 66 |
| none | abstention | core | 6 | 437 | 110 | 49 | 28 |
| oracle | abstention | core | 6 | 437 | 110 | 49 | 28 |
| pull | abstention | core | 4 | 589 | 148 | 66 | 37 |
| push_breadcrumb | abstention | core | 6 | 437 | 110 | 49 | 28 |
| push_pages_upper_bound | abstention | core | 6 | 437 | 110 | 49 | 28 |
| native_grep | abstention | medium | 4 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| native_index | abstention | medium | 5 | 503 | 126 | 56 | 32 |
| none | abstention | medium | 5 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| oracle | abstention | medium | 5 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| pull | abstention | medium | 4 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| push_breadcrumb | abstention | medium | 5 | 754 | 189 | 84 | 48 |
| push_pages_upper_bound | abstention | medium | 5 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| native_grep | aggregation | core | 5 | 754 | 189 | 84 | 48 |
| native_index | aggregation | core | 6 | 698 | 175 | 78 | 44 |
| none | aggregation | core | 6 | 698 | 175 | 78 | 44 |
| oracle | aggregation | core | 6 | 1745 | 437 | 194 | 110 |
| pull | aggregation | core | 6 | 437 | 110 | 49 | 28 |
| push_breadcrumb | aggregation | core | 6 | 698 | 175 | 78 | 44 |
| push_pages_upper_bound | aggregation | core | 6 | 698 | 175 | 78 | 44 |
| native_grep | aggregation | medium | 2 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| native_index | aggregation | medium | 6 | 1483 | 371 | 165 | 93 |
| none | aggregation | medium | 6 | 437 | 110 | 49 | 28 |
| oracle | aggregation | medium | 6 | 698 | 175 | 78 | 44 |
| pull | aggregation | medium | 5 | 503 | 126 | 56 | 32 |
| push_breadcrumb | aggregation | medium | 6 | 437 | 110 | 49 | 28 |
| push_pages_upper_bound | aggregation | medium | 6 | 437 | 110 | 49 | 28 |
| native_grep | contradiction | core | 6 | 698 | 175 | 78 | 44 |
| native_index | contradiction | core | 4 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| none | contradiction | core | 6 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| oracle | contradiction | core | 6 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| pull | contradiction | core | 6 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| push_breadcrumb | contradiction | core | 6 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| push_pages_upper_bound | contradiction | core | 6 | 698 | 175 | 78 | 44 |
| native_grep | contradiction | medium | 6 | 437 | 110 | 49 | 28 |
| native_index | contradiction | medium | 6 | 437 | 110 | 49 | 28 |
| none | contradiction | medium | 6 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| oracle | contradiction | medium | 6 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| pull | contradiction | medium | 6 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| push_breadcrumb | contradiction | medium | 6 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| push_pages_upper_bound | contradiction | medium | 6 | 698 | 175 | 78 | 44 |
| native_grep | disambiguation | core | 8 | 736 | 184 | 82 | 46 |
| native_index | disambiguation | core | 8 | 736 | 184 | 82 | 46 |
| none | disambiguation | core | 8 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| oracle | disambiguation | core | 8 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| pull | disambiguation | core | 8 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| push_breadcrumb | disambiguation | core | 8 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| push_pages_upper_bound | disambiguation | core | 8 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| native_grep | disambiguation | medium | 8 | 736 | 184 | 82 | 46 |
| native_index | disambiguation | medium | 8 | 589 | 148 | 66 | 37 |
| none | disambiguation | medium | 8 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| oracle | disambiguation | medium | 8 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| pull | disambiguation | medium | 8 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| push_breadcrumb | disambiguation | medium | 8 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| push_pages_upper_bound | disambiguation | medium | 8 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| native_grep | distractor_robustness | core | 4 | 589 | 148 | 66 | 37 |
| native_index | distractor_robustness | core | 4 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| none | distractor_robustness | core | 4 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| oracle | distractor_robustness | core | 4 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| pull | distractor_robustness | core | 4 | 589 | 148 | 66 | 37 |
| push_breadcrumb | distractor_robustness | core | 4 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| push_pages_upper_bound | distractor_robustness | core | 4 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| native_grep | distractor_robustness | medium | 4 | 785 | 197 | 88 | 50 |
| native_index | distractor_robustness | medium | 4 | 589 | 148 | 66 | 37 |
| none | distractor_robustness | medium | 4 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| oracle | distractor_robustness | medium | 4 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| pull | distractor_robustness | medium | 4 | 785 | 197 | 88 | 50 |
| push_breadcrumb | distractor_robustness | medium | 4 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| push_pages_upper_bound | distractor_robustness | medium | 4 | 785 | 197 | 88 | 50 |
| native_grep | follow_through | core | 10 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| native_index | follow_through | core | 10 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| none | follow_through | core | 11 | 260 | 65 | 29 | 17 |
| oracle | follow_through | core | 11 | 260 | 65 | 29 | 17 |
| pull | follow_through | core | 11 | 260 | 65 | 29 | 17 |
| push_breadcrumb | follow_through | core | 11 | 260 | 65 | 29 | 17 |
| push_pages_upper_bound | follow_through | core | 11 | 260 | 65 | 29 | 17 |
| native_grep | follow_through | medium | 5 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| native_index | follow_through | medium | 12 | 240 | 60 | 27 | 15 |
| none | follow_through | medium | 12 | 240 | 60 | 27 | 15 |
| oracle | follow_through | medium | 12 | 240 | 60 | 27 | 15 |
| pull | follow_through | medium | 11 | 260 | 65 | 29 | 17 |
| push_breadcrumb | follow_through | medium | 12 | 240 | 60 | 27 | 15 |
| push_pages_upper_bound | follow_through | medium | 12 | 240 | 60 | 27 | 15 |
| native_grep | multi_hop | core | 4 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| native_index | multi_hop | core | 5 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| none | multi_hop | core | 5 | 503 | 126 | 56 | 32 |
| oracle | multi_hop | core | 5 | 1256 | 314 | 140 | 79 |
| pull | multi_hop | core | 4 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| push_breadcrumb | multi_hop | core | 5 | 503 | 126 | 56 | 32 |
| push_pages_upper_bound | multi_hop | core | 5 | 503 | 126 | 56 | 32 |
| native_grep | multi_hop | medium | 3 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| native_index | multi_hop | medium | 6 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| none | multi_hop | medium | 6 | 785 | 197 | 88 | 50 |
| oracle | multi_hop | medium | 6 | 1745 | 437 | 194 | 110 |
| pull | multi_hop | medium | 3 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| push_breadcrumb | multi_hop | medium | 6 | 785 | 197 | 88 | 50 |
| push_pages_upper_bound | multi_hop | medium | 6 | 785 | 197 | 88 | 50 |
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
| native_grep | redundancy | core | 8 | 1570 | 393 | 175 | 99 |
| native_index | redundancy | core | 8 | 736 | 184 | 82 | 46 |
| none | redundancy | core | 8 | 589 | 148 | 66 | 37 |
| oracle | redundancy | core | 8 | 589 | 148 | 66 | 37 |
| pull | redundancy | core | 8 | 1129 | 283 | 126 | 71 |
| push_breadcrumb | redundancy | core | 8 | 589 | 148 | 66 | 37 |
| push_pages_upper_bound | redundancy | core | 8 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| native_grep | redundancy | medium | 8 | 344 | 86 | 39 | 22 |
| native_index | redundancy | medium | 8 | 589 | 148 | 66 | 37 |
| none | redundancy | medium | 8 | 344 | 86 | 39 | 22 |
| oracle | redundancy | medium | 8 | 589 | 148 | 66 | 37 |
| pull | redundancy | medium | 8 | 344 | 86 | 39 | 22 |
| push_breadcrumb | redundancy | medium | 8 | 344 | 86 | 39 | 22 |
| push_pages_upper_bound | redundancy | medium | 8 | 785 | 197 | 88 | 50 |
| native_grep | single_hop | core | 16 | 344 | 86 | 39 | 22 |
| native_index | single_hop | core | 16 | 785 | 197 | 88 | 50 |
| none | single_hop | core | 16 | 479 | 120 | 54 | 30 |
| oracle | single_hop | core | 16 | 1129 | 283 | 126 | 71 |
| pull | single_hop | core | 16 | 184 | 46 | 21 | 12 |
| push_breadcrumb | single_hop | core | 16 | 479 | 120 | 54 | 30 |
| push_pages_upper_bound | single_hop | core | 16 | 773 | 194 | 86 | 49 |
| native_grep | single_hop | medium | 16 | 344 | 86 | 39 | 22 |
| native_index | single_hop | medium | 16 | 344 | 86 | 39 | 22 |
| none | single_hop | medium | 16 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| oracle | single_hop | medium | 16 | 589 | 148 | 66 | 37 |
| pull | single_hop | medium | 16 | 184 | 46 | 21 | 12 |
| push_breadcrumb | single_hop | medium | 16 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| push_pages_upper_bound | single_hop | medium | 16 | 736 | 184 | 82 | 46 |
| native_grep | temporal | core | 12 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| native_index | temporal | core | 12 | 764 | 191 | 85 | 48 |
| none | temporal | core | 12 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| oracle | temporal | core | 12 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| pull | temporal | core | 12 | 240 | 60 | 27 | 15 |
| push_breadcrumb | temporal | core | 12 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| push_pages_upper_bound | temporal | core | 12 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| native_grep | temporal | medium | 11 | 623 | 156 | 70 | 39 |
| native_index | temporal | medium | 12 | 698 | 175 | 78 | 44 |
| none | temporal | medium | 12 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| oracle | temporal | medium | 12 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| pull | temporal | medium | 12 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| push_breadcrumb | temporal | medium | 12 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
| push_pages_upper_bound | temporal | medium | 12 | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) | n/a (zero discordance observed -- cannot extrapolate) |
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
| native_grep | 204 | 118 | 0.578 | 24 |
| native_index | 222 | 121 | 0.545 | 6 |
| none | 228 | 12 | 0.053 | 0 |
| oracle | 228 | 173 | 0.759 | 0 |
| pull | 217 | 149 | 0.687 | 11 |
| push_breadcrumb | 220 | 9 | 0.041 | 8 |
| push_breadcrumb_pull | 217 | 153 | 0.705 | 11 |
| push_pages_upper_bound | 228 | 99 | 0.434 | 0 |

Correctness per (arm, probe_class, corpus_scale):

| arm | probe_class | scale | n | correct | rate | harness failures |
|---|---|---|---|---|---|---|
| native_grep | abstention | core | 3 | 2 | 0.667 | 3 |
| native_grep | abstention | medium | 4 | 4 | 1.000 | 2 |
| native_grep | aggregation | core | 5 | 5 | 1.000 | 1 |
| native_grep | aggregation | medium | 2 | 2 | 1.000 | 4 |
| native_grep | contradiction | core | 6 | 4 | 0.667 | 0 |
| native_grep | contradiction | medium | 6 | 5 | 0.833 | 0 |
| native_grep | disambiguation | core | 8 | 5 | 0.625 | 0 |
| native_grep | disambiguation | medium | 8 | 5 | 0.625 | 0 |
| native_grep | distractor_robustness | core | 4 | 1 | 0.250 | 0 |
| native_grep | distractor_robustness | medium | 4 | 2 | 0.500 | 0 |
| native_grep | follow_through | core | 11 | 9 | 0.818 | 1 |
| native_grep | follow_through | medium | 5 | 5 | 1.000 | 7 |
| native_grep | multi_hop | core | 5 | 5 | 1.000 | 1 |
| native_grep | multi_hop | medium | 3 | 2 | 0.667 | 3 |
| native_grep | negative_knowledge | core | 6 | 6 | 1.000 | 0 |
| native_grep | negative_knowledge | medium | 5 | 4 | 0.800 | 1 |
| native_grep | redundancy | core | 8 | 2 | 0.250 | 0 |
| native_grep | redundancy | medium | 8 | 0 | 0.000 | 0 |
| native_grep | single_hop | core | 16 | 15 | 0.938 | 0 |
| native_grep | single_hop | medium | 16 | 14 | 0.875 | 0 |
| native_grep | temporal | core | 12 | 12 | 1.000 | 0 |
| native_grep | temporal | medium | 11 | 8 | 0.727 | 1 |
| native_grep | unprompted_push | core | 24 | 0 | 0.000 | 0 |
| native_grep | unprompted_push | medium | 24 | 1 | 0.042 | 0 |
| native_index | abstention | core | 6 | 5 | 0.833 | 0 |
| native_index | abstention | medium | 6 | 5 | 0.833 | 0 |
| native_index | aggregation | core | 6 | 0 | 0.000 | 0 |
| native_index | aggregation | medium | 6 | 2 | 0.333 | 0 |
| native_index | contradiction | core | 4 | 4 | 1.000 | 2 |
| native_index | contradiction | medium | 6 | 5 | 0.833 | 0 |
| native_index | disambiguation | core | 8 | 5 | 0.625 | 0 |
| native_index | disambiguation | medium | 8 | 2 | 0.250 | 0 |
| native_index | distractor_robustness | core | 4 | 0 | 0.000 | 0 |
| native_index | distractor_robustness | medium | 4 | 1 | 0.250 | 0 |
| native_index | follow_through | core | 11 | 10 | 0.909 | 1 |
| native_index | follow_through | medium | 12 | 12 | 1.000 | 0 |
| native_index | multi_hop | core | 6 | 4 | 0.667 | 0 |
| native_index | multi_hop | medium | 6 | 3 | 0.500 | 0 |
| native_index | negative_knowledge | core | 6 | 4 | 0.667 | 0 |
| native_index | negative_knowledge | medium | 6 | 5 | 0.833 | 0 |
| native_index | redundancy | core | 8 | 1 | 0.125 | 0 |
| native_index | redundancy | medium | 8 | 1 | 0.125 | 0 |
| native_index | single_hop | core | 16 | 13 | 0.812 | 0 |
| native_index | single_hop | medium | 16 | 14 | 0.875 | 0 |
| native_index | temporal | core | 12 | 7 | 0.583 | 0 |
| native_index | temporal | medium | 12 | 8 | 0.667 | 0 |
| native_index | unprompted_push | core | 23 | 6 | 0.261 | 1 |
| native_index | unprompted_push | medium | 22 | 4 | 0.182 | 2 |
| none | abstention | core | 6 | 6 | 1.000 | 0 |
| none | abstention | medium | 6 | 6 | 1.000 | 0 |
| none | aggregation | core | 6 | 0 | 0.000 | 0 |
| none | aggregation | medium | 6 | 0 | 0.000 | 0 |
| none | contradiction | core | 6 | 0 | 0.000 | 0 |
| none | contradiction | medium | 6 | 0 | 0.000 | 0 |
| none | disambiguation | core | 8 | 0 | 0.000 | 0 |
| none | disambiguation | medium | 8 | 0 | 0.000 | 0 |
| none | distractor_robustness | core | 4 | 0 | 0.000 | 0 |
| none | distractor_robustness | medium | 4 | 0 | 0.000 | 0 |
| none | follow_through | core | 12 | 0 | 0.000 | 0 |
| none | follow_through | medium | 12 | 0 | 0.000 | 0 |
| none | multi_hop | core | 6 | 0 | 0.000 | 0 |
| none | multi_hop | medium | 6 | 0 | 0.000 | 0 |
| none | negative_knowledge | core | 6 | 0 | 0.000 | 0 |
| none | negative_knowledge | medium | 6 | 0 | 0.000 | 0 |
| none | redundancy | core | 8 | 0 | 0.000 | 0 |
| none | redundancy | medium | 8 | 0 | 0.000 | 0 |
| none | single_hop | core | 16 | 0 | 0.000 | 0 |
| none | single_hop | medium | 16 | 0 | 0.000 | 0 |
| none | temporal | core | 12 | 0 | 0.000 | 0 |
| none | temporal | medium | 12 | 0 | 0.000 | 0 |
| none | unprompted_push | core | 24 | 0 | 0.000 | 0 |
| none | unprompted_push | medium | 24 | 0 | 0.000 | 0 |
| oracle | abstention | core | 6 | 6 | 1.000 | 0 |
| oracle | abstention | medium | 6 | 6 | 1.000 | 0 |
| oracle | aggregation | core | 6 | 4 | 0.667 | 0 |
| oracle | aggregation | medium | 6 | 3 | 0.500 | 0 |
| oracle | contradiction | core | 6 | 6 | 1.000 | 0 |
| oracle | contradiction | medium | 6 | 6 | 1.000 | 0 |
| oracle | disambiguation | core | 8 | 8 | 1.000 | 0 |
| oracle | disambiguation | medium | 8 | 8 | 1.000 | 0 |
| oracle | distractor_robustness | core | 4 | 4 | 1.000 | 0 |
| oracle | distractor_robustness | medium | 4 | 4 | 1.000 | 0 |
| oracle | follow_through | core | 12 | 12 | 1.000 | 0 |
| oracle | follow_through | medium | 12 | 12 | 1.000 | 0 |
| oracle | multi_hop | core | 6 | 5 | 0.833 | 0 |
| oracle | multi_hop | medium | 6 | 5 | 0.833 | 0 |
| oracle | negative_knowledge | core | 6 | 5 | 0.833 | 0 |
| oracle | negative_knowledge | medium | 6 | 5 | 0.833 | 0 |
| oracle | redundancy | core | 8 | 4 | 0.500 | 0 |
| oracle | redundancy | medium | 8 | 5 | 0.625 | 0 |
| oracle | single_hop | core | 16 | 11 | 0.688 | 0 |
| oracle | single_hop | medium | 16 | 12 | 0.750 | 0 |
| oracle | temporal | core | 12 | 12 | 1.000 | 0 |
| oracle | temporal | medium | 12 | 12 | 1.000 | 0 |
| oracle | unprompted_push | core | 24 | 9 | 0.375 | 0 |
| oracle | unprompted_push | medium | 24 | 9 | 0.375 | 0 |
| pull | abstention | core | 4 | 4 | 1.000 | 2 |
| pull | abstention | medium | 4 | 4 | 1.000 | 2 |
| pull | aggregation | core | 6 | 1 | 0.167 | 0 |
| pull | aggregation | medium | 5 | 0 | 0.000 | 1 |
| pull | contradiction | core | 6 | 6 | 1.000 | 0 |
| pull | contradiction | medium | 6 | 6 | 1.000 | 0 |
| pull | disambiguation | core | 8 | 8 | 1.000 | 0 |
| pull | disambiguation | medium | 8 | 8 | 1.000 | 0 |
| pull | distractor_robustness | core | 4 | 3 | 0.750 | 0 |
| pull | distractor_robustness | medium | 4 | 2 | 0.500 | 0 |
| pull | follow_through | core | 12 | 12 | 1.000 | 0 |
| pull | follow_through | medium | 11 | 11 | 1.000 | 1 |
| pull | multi_hop | core | 4 | 4 | 1.000 | 2 |
| pull | multi_hop | medium | 3 | 2 | 0.667 | 3 |
| pull | negative_knowledge | core | 6 | 5 | 0.833 | 0 |
| pull | negative_knowledge | medium | 6 | 5 | 0.833 | 0 |
| pull | redundancy | core | 8 | 7 | 0.875 | 0 |
| pull | redundancy | medium | 8 | 6 | 0.750 | 0 |
| pull | single_hop | core | 16 | 14 | 0.875 | 0 |
| pull | single_hop | medium | 16 | 15 | 0.938 | 0 |
| pull | temporal | core | 12 | 11 | 0.917 | 0 |
| pull | temporal | medium | 12 | 12 | 1.000 | 0 |
| pull | unprompted_push | core | 24 | 0 | 0.000 | 0 |
| pull | unprompted_push | medium | 24 | 3 | 0.125 | 0 |
| push_breadcrumb | abstention | core | 6 | 6 | 1.000 | 0 |
| push_breadcrumb | abstention | medium | 6 | 3 | 0.500 | 0 |
| push_breadcrumb | aggregation | core | 6 | 0 | 0.000 | 0 |
| push_breadcrumb | aggregation | medium | 6 | 0 | 0.000 | 0 |
| push_breadcrumb | contradiction | core | 6 | 0 | 0.000 | 0 |
| push_breadcrumb | contradiction | medium | 6 | 0 | 0.000 | 0 |
| push_breadcrumb | disambiguation | core | 8 | 0 | 0.000 | 0 |
| push_breadcrumb | disambiguation | medium | 8 | 0 | 0.000 | 0 |
| push_breadcrumb | distractor_robustness | core | 4 | 0 | 0.000 | 0 |
| push_breadcrumb | distractor_robustness | medium | 4 | 0 | 0.000 | 0 |
| push_breadcrumb | follow_through | core | 12 | 0 | 0.000 | 0 |
| push_breadcrumb | follow_through | medium | 12 | 0 | 0.000 | 0 |
| push_breadcrumb | multi_hop | core | 6 | 0 | 0.000 | 0 |
| push_breadcrumb | multi_hop | medium | 6 | 0 | 0.000 | 0 |
| push_breadcrumb | negative_knowledge | core | 6 | 0 | 0.000 | 0 |
| push_breadcrumb | negative_knowledge | medium | 2 | 0 | 0.000 | 4 |
| push_breadcrumb | redundancy | core | 8 | 0 | 0.000 | 0 |
| push_breadcrumb | redundancy | medium | 8 | 0 | 0.000 | 0 |
| push_breadcrumb | single_hop | core | 16 | 0 | 0.000 | 0 |
| push_breadcrumb | single_hop | medium | 16 | 0 | 0.000 | 0 |
| push_breadcrumb | temporal | core | 12 | 0 | 0.000 | 0 |
| push_breadcrumb | temporal | medium | 12 | 0 | 0.000 | 0 |
| push_breadcrumb | unprompted_push | core | 24 | 0 | 0.000 | 0 |
| push_breadcrumb | unprompted_push | medium | 20 | 0 | 0.000 | 4 |
| push_breadcrumb_pull | abstention | core | 6 | 5 | 0.833 | 0 |
| push_breadcrumb_pull | abstention | medium | 5 | 5 | 1.000 | 1 |
| push_breadcrumb_pull | aggregation | core | 6 | 2 | 0.333 | 0 |
| push_breadcrumb_pull | aggregation | medium | 6 | 1 | 0.167 | 0 |
| push_breadcrumb_pull | contradiction | core | 6 | 6 | 1.000 | 0 |
| push_breadcrumb_pull | contradiction | medium | 6 | 6 | 1.000 | 0 |
| push_breadcrumb_pull | disambiguation | core | 8 | 8 | 1.000 | 0 |
| push_breadcrumb_pull | disambiguation | medium | 8 | 8 | 1.000 | 0 |
| push_breadcrumb_pull | distractor_robustness | core | 4 | 4 | 1.000 | 0 |
| push_breadcrumb_pull | distractor_robustness | medium | 4 | 4 | 1.000 | 0 |
| push_breadcrumb_pull | follow_through | core | 11 | 10 | 0.909 | 1 |
| push_breadcrumb_pull | follow_through | medium | 12 | 11 | 0.917 | 0 |
| push_breadcrumb_pull | multi_hop | core | 5 | 4 | 0.800 | 1 |
| push_breadcrumb_pull | multi_hop | medium | 6 | 3 | 0.500 | 0 |
| push_breadcrumb_pull | negative_knowledge | core | 6 | 4 | 0.667 | 0 |
| push_breadcrumb_pull | negative_knowledge | medium | 2 | 2 | 1.000 | 4 |
| push_breadcrumb_pull | redundancy | core | 8 | 6 | 0.750 | 0 |
| push_breadcrumb_pull | redundancy | medium | 8 | 7 | 0.875 | 0 |
| push_breadcrumb_pull | single_hop | core | 16 | 13 | 0.812 | 0 |
| push_breadcrumb_pull | single_hop | medium | 16 | 16 | 1.000 | 0 |
| push_breadcrumb_pull | temporal | core | 12 | 12 | 1.000 | 0 |
| push_breadcrumb_pull | temporal | medium | 12 | 12 | 1.000 | 0 |
| push_breadcrumb_pull | unprompted_push | core | 24 | 3 | 0.125 | 0 |
| push_breadcrumb_pull | unprompted_push | medium | 20 | 1 | 0.050 | 4 |
| push_pages_upper_bound | abstention | core | 6 | 6 | 1.000 | 0 |
| push_pages_upper_bound | abstention | medium | 6 | 6 | 1.000 | 0 |
| push_pages_upper_bound | aggregation | core | 6 | 0 | 0.000 | 0 |
| push_pages_upper_bound | aggregation | medium | 6 | 0 | 0.000 | 0 |
| push_pages_upper_bound | contradiction | core | 6 | 2 | 0.333 | 0 |
| push_pages_upper_bound | contradiction | medium | 6 | 2 | 0.333 | 0 |
| push_pages_upper_bound | disambiguation | core | 8 | 8 | 1.000 | 0 |
| push_pages_upper_bound | disambiguation | medium | 8 | 8 | 1.000 | 0 |
| push_pages_upper_bound | distractor_robustness | core | 4 | 4 | 1.000 | 0 |
| push_pages_upper_bound | distractor_robustness | medium | 4 | 2 | 0.500 | 0 |
| push_pages_upper_bound | follow_through | core | 12 | 0 | 0.000 | 0 |
| push_pages_upper_bound | follow_through | medium | 12 | 0 | 0.000 | 0 |
| push_pages_upper_bound | multi_hop | core | 6 | 0 | 0.000 | 0 |
| push_pages_upper_bound | multi_hop | medium | 6 | 0 | 0.000 | 0 |
| push_pages_upper_bound | negative_knowledge | core | 6 | 6 | 1.000 | 0 |
| push_pages_upper_bound | negative_knowledge | medium | 6 | 6 | 1.000 | 0 |
| push_pages_upper_bound | redundancy | core | 8 | 6 | 0.750 | 0 |
| push_pages_upper_bound | redundancy | medium | 8 | 7 | 0.875 | 0 |
| push_pages_upper_bound | single_hop | core | 16 | 6 | 0.375 | 0 |
| push_pages_upper_bound | single_hop | medium | 16 | 6 | 0.375 | 0 |
| push_pages_upper_bound | temporal | core | 12 | 12 | 1.000 | 0 |
| push_pages_upper_bound | temporal | medium | 12 | 12 | 1.000 | 0 |
| push_pages_upper_bound | unprompted_push | core | 24 | 0 | 0.000 | 0 |
| push_pages_upper_bound | unprompted_push | medium | 24 | 0 | 0.000 | 0 |

Every `oracle` probe graded incorrect (55 probes):

| probe_class | scale | replicate | probe_id | push_breadcrumb_pull correct |
|---|---|---|---|---|
| aggregation | core | 0 | aggregation_inhouse_tools | False |
| aggregation | core | 1 | aggregation_inhouse_tools | True |
| aggregation | medium | 1 | aggregation_delivery_roles | False |
| aggregation | medium | 0 | aggregation_inhouse_tools | False |
| aggregation | medium | 1 | aggregation_inhouse_tools | False |
| multi_hop | core | 0 | spend_approver_named | True |
| multi_hop | medium | 1 | spend_approver_named | True |
| negative_knowledge | core | 0 | vendor_migration_rerun_lesson | False |
| negative_knowledge | medium | 1 | vendor_migration_rerun_lesson | harness_failure |
| redundancy | core | 0 | keelbridge_programme_scope | False |
| redundancy | core | 1 | keelbridge_programme_scope | False |
| redundancy | core | 0 | mira_castellane_role | True |
| redundancy | core | 1 | mira_castellane_role | True |
| redundancy | medium | 0 | keelbridge_programme_scope | True |
| redundancy | medium | 1 | keelbridge_programme_scope | False |
| redundancy | medium | 1 | mira_castellane_role | True |
| single_hop | core | 0 | bramfield_retainer_renewal | True |
| single_hop | core | 1 | bramfield_retainer_renewal | True |
| single_hop | core | 1 | confidentiality_rule | True |
| single_hop | core | 1 | lighthouse_migration_rollback | False |
| single_hop | core | 0 | triform_vendor_consolidation | True |
| single_hop | medium | 0 | bramfield_retainer_renewal | True |
| single_hop | medium | 1 | bramfield_retainer_renewal | True |
| single_hop | medium | 0 | triform_vendor_consolidation | True |
| single_hop | medium | 1 | triform_vendor_consolidation | True |
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

57 probes have at least one row; 57 are complete across every (scale, replicate, arm) this store carries. 0 probes have no row at all.

## 7. Actual spend for this run

Priced at `claude-haiku-4-5-20251001` rates with the cache multipliers `athenaeum.models` documents (1.25x on a cache write, 0.1x on a cache read), over EVERY persisted row -- a cell that tripped the turn cap was paid for exactly like one that answered.

| rows counted | input | cache write | cache read | output | billable input | USD |
|---|---|---|---|---|---|---|
| 1824 | 14,355,896 | 0 | 0 | 558,756 | 14,355,896 | $17.15 |

Total tokens: 14,914,652.
