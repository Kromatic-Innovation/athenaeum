<!-- SPDX-License-Identifier: Apache-2.0 -->

<!--
Rendered north-star grid report, Actions run 42169666857 (2026-11-06, `hestia/2309-measure-the-north-star-grid-noise-floor` @ 6d8dbbd7183f), recorded in-tree for issue athenaeum#2309 because the Actions artifact expires 30 days after the run -- the same reason athenaeum#1854 recorded its own. PARTIAL: this run tripped its token ceiling at 1360 of 1824 cells. Byte-identical to the artifact's own `north-star-2026-11-06.md` (sha256 in manifest.md) apart from this comment.
-->

> **PARTIAL RUN** — stopped early.
> abort_reason: SpendCeilingExceededError: rollout run exceeded token ceiling (11014424 > 10842105) mid-grid -- stopped every worker. Groups not yet started never run, and a group already in flight stops at its next arm boundary, so the overshoot is at most one cell per worker. Shrink the grid or the --scale tier, or raise the ceiling deliberately with --max-tokens (or a larger --max-spend, which derives it). (/home/runner/work/athenaeum/athenaeum/tests/evals/north_star_cli.py:1539)

# North-star report: free dimensions only (athenaeum#1523)

- generated: 2026-11-06T06:12:40Z
- athenaeum_version: 0.23.4
- git_sha: 6d8dbbd7183f
- search_backend: vector
- hybrid: on
- relevance_floor_vector: off
- relevance_floor_fts5: off
- preflight: ok provider=api probe=pto_allowance arm=push_breadcrumb_pull recall_called=true tool_calls=1
- corpus_digest[core]: 3c5ea876688b089e
- corpus_digest[medium]: 42e6ce0dfdc53e02
- grader_revision: athenaeum#1935
- total rollout rows: 1360
- harness failures: 40
  - turn_cap[arm=native_grep, scale=core]: 6
  - turn_cap[arm=native_grep, scale=medium]: 17
  - turn_cap[arm=native_index, scale=core]: 3
  - turn_cap[arm=pull, scale=core]: 4
  - turn_cap[arm=pull, scale=medium]: 7
  - turn_cap[arm=push_breadcrumb_pull, scale=core]: 2
  - turn_cap[arm=push_breadcrumb_pull, scale=medium]: 1

No LLM judge is invoked anywhere on this path — every figure below is a deterministic computation over already-captured rollout transcripts. This is a **measurement, not a regression gate**; nothing here should ever fail a build.

> **partial: 1360 of 1824 cells** — the grid did not finish, so every figure below is computed over the cells that did. Read the verdicts as provisional.

## Decision (design doc §7, athenaeum#1734)

**Verdict arm:** `push_breadcrumb_pull` (ruling R1) -- the shipped configuration is the ONE Athenaeum arm every condition below reads. Other Athenaeum arms still appear in the per-dimension tables further down, but never in these verdicts -- no picking the most-correct arm for conditions 1-2 and the cheapest for condition 3.

Three conditions, evaluated per scale, all against the verdict arm above: **(1)** win the relationship use case (single_hop/multi_hop/disambiguation/temporal probes targeting person/company pages), one POOLED correctness rate over the whole subset, against the better native arm's own pooled rate on the same rows; **(2)** do not lose any other current use case, class by class, skipping (and naming) any class where either side has no gradable rows; **(3)** cost per correct answer within budget of the better native arm, per class, skipping (and naming) any class with no native cost data at all (ruling R5) -- `>2.0x fail`, `<=2.0x limit`, `<=1.0x target`, `<=0.5x aspirational`, or `native-zero` (a pass) when the better native arm scored zero correct answers while the verdict arm scored at least one.

report-only classes excluded: aggregation, contradiction, negative_knowledge, unprompted_push -- excluded from conditions 2 and 3 entirely (issue athenaeum#1776); promotion is an explicit operator ruling on athenaeum#1736's thread, recorded by editing `tests.evals.corpus.CONDITION_2_ENROLLED`, never a side effect of this report.

**"Better native arm"** means two different things across these conditions, both the harshest reading available to Athenaeum: for condition 1, the native arm with the HIGHER pooled correctness rate; for condition 3, the native arm with the CHEAPER DEFINED cost per correct (a native arm that scored zero correct answers has no defined cost and is never picked as "cheaper" by that alone -- see `native-zero`).

**Cutoff scale:** `none` -- no scale at or above `medium` passed all three conditions. Failing condition per scale:

- `core`: condition 2: compared 8 classes, skipped 0; worse than native on 'multi_hop' (0.000 < 1.000); condition 3: cost compared 8 classes, skipped 0; worst reading is 'fail' for 'abstention' (ratio=5.023)
- `medium`: condition 2: compared 8 classes, skipped 0; worse than native on 'follow_through' (0.917 < 1.000); condition 3: cost compared 8 classes, skipped 0; worst reading is 'fail' for 'abstention' (ratio=4.522)

| scale | cutoff eligible | condition 1 | condition 2 | condition 3 | reading | all pass |
| --- | --- | --- | --- | --- | --- | --- |
| core | no | pass | fail | fail | fail | fail |
| medium | yes | pass | fail | fail | fail | fail |

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
| abstain_unknown_client | native_grep | core | 0 | api | n/a |
| abstain_unknown_client | native_grep | core | 1 | api | n/a |
| abstain_unknown_client | native_grep | medium | 0 | api | n/a |
| abstain_unknown_client | native_grep | medium | 1 | api | n/a |
| abstain_unknown_client | native_index | core | 0 | api | n/a |
| abstain_unknown_client | native_index | core | 1 | api | n/a |
| abstain_unknown_client | native_index | medium | 0 | api | n/a |
| abstain_unknown_client | native_index | medium | 1 | api | n/a |
| abstain_unknown_client | none | core | 0 | api | n/a |
| abstain_unknown_client | none | core | 1 | api | n/a |
| abstain_unknown_client | none | medium | 0 | api | n/a |
| abstain_unknown_client | none | medium | 1 | api | n/a |
| abstain_unknown_client | oracle | core | 0 | api | n/a |
| abstain_unknown_client | oracle | core | 1 | api | n/a |
| abstain_unknown_client | oracle | medium | 0 | api | n/a |
| abstain_unknown_client | oracle | medium | 1 | api | n/a |
| abstain_unknown_client | pull | core | 0 | api | n/a |
| abstain_unknown_client | pull | core | 1 | api | n/a |
| abstain_unknown_client | pull | medium | 0 | api | n/a |
| abstain_unknown_client | pull | medium | 1 | api | n/a |
| abstain_unknown_client | push_breadcrumb | core | 0 | api | n/a |
| abstain_unknown_client | push_breadcrumb | core | 1 | api | n/a |
| abstain_unknown_client | push_breadcrumb | medium | 0 | api | n/a |
| abstain_unknown_client | push_breadcrumb | medium | 1 | api | n/a |
| abstain_unknown_client | push_breadcrumb_pull | core | 0 | api | n/a |
| abstain_unknown_client | push_breadcrumb_pull | core | 1 | api | n/a |
| abstain_unknown_client | push_breadcrumb_pull | medium | 0 | api | n/a |
| abstain_unknown_client | push_breadcrumb_pull | medium | 1 | api | n/a |
| abstain_unknown_client | push_pages_upper_bound | core | 0 | api | n/a |
| abstain_unknown_client | push_pages_upper_bound | core | 1 | api | n/a |
| abstain_unknown_client | push_pages_upper_bound | medium | 0 | api | n/a |
| abstain_unknown_client | push_pages_upper_bound | medium | 1 | api | n/a |
| abstain_unknown_person | native_grep | core | 0 | api | n/a |
| abstain_unknown_person | native_grep | core | 1 | api | n/a |
| abstain_unknown_person | native_grep | medium | 0 | api | n/a |
| abstain_unknown_person | native_grep | medium | 1 | api | n/a |
| abstain_unknown_person | native_index | core | 0 | api | n/a |
| abstain_unknown_person | native_index | core | 1 | api | n/a |
| abstain_unknown_person | native_index | medium | 0 | api | n/a |
| abstain_unknown_person | native_index | medium | 1 | api | n/a |
| abstain_unknown_person | none | core | 0 | api | n/a |
| abstain_unknown_person | none | core | 1 | api | n/a |
| abstain_unknown_person | none | medium | 0 | api | n/a |
| abstain_unknown_person | none | medium | 1 | api | n/a |
| abstain_unknown_person | oracle | core | 0 | api | n/a |
| abstain_unknown_person | oracle | core | 1 | api | n/a |
| abstain_unknown_person | oracle | medium | 0 | api | n/a |
| abstain_unknown_person | oracle | medium | 1 | api | n/a |
| abstain_unknown_person | pull | core | 0 | api | n/a |
| abstain_unknown_person | pull | core | 1 | api | n/a |
| abstain_unknown_person | pull | medium | 0 | api | n/a |
| abstain_unknown_person | pull | medium | 1 | api | n/a |
| abstain_unknown_person | push_breadcrumb | core | 0 | api | n/a |
| abstain_unknown_person | push_breadcrumb | core | 1 | api | n/a |
| abstain_unknown_person | push_breadcrumb | medium | 0 | api | n/a |
| abstain_unknown_person | push_breadcrumb | medium | 1 | api | n/a |
| abstain_unknown_person | push_breadcrumb_pull | core | 0 | api | n/a |
| abstain_unknown_person | push_breadcrumb_pull | core | 1 | api | n/a |
| abstain_unknown_person | push_breadcrumb_pull | medium | 0 | api | n/a |
| abstain_unknown_person | push_breadcrumb_pull | medium | 1 | api | n/a |
| abstain_unknown_person | push_pages_upper_bound | core | 0 | api | n/a |
| abstain_unknown_person | push_pages_upper_bound | core | 1 | api | n/a |
| abstain_unknown_person | push_pages_upper_bound | medium | 0 | api | n/a |
| abstain_unknown_person | push_pages_upper_bound | medium | 1 | api | n/a |
| abstain_unknown_policy | native_grep | core | 0 | api | n/a |
| abstain_unknown_policy | native_grep | core | 1 | api | n/a |
| abstain_unknown_policy | native_grep | medium | 0 | api | n/a |
| abstain_unknown_policy | native_grep | medium | 1 | api | n/a |
| abstain_unknown_policy | native_index | core | 0 | api | n/a |
| abstain_unknown_policy | native_index | core | 1 | api | n/a |
| abstain_unknown_policy | native_index | medium | 0 | api | n/a |
| abstain_unknown_policy | native_index | medium | 1 | api | n/a |
| abstain_unknown_policy | none | core | 0 | api | n/a |
| abstain_unknown_policy | none | core | 1 | api | n/a |
| abstain_unknown_policy | none | medium | 0 | api | n/a |
| abstain_unknown_policy | none | medium | 1 | api | n/a |
| abstain_unknown_policy | oracle | core | 0 | api | n/a |
| abstain_unknown_policy | oracle | core | 1 | api | n/a |
| abstain_unknown_policy | oracle | medium | 0 | api | n/a |
| abstain_unknown_policy | oracle | medium | 1 | api | n/a |
| abstain_unknown_policy | pull | core | 0 | api | n/a |
| abstain_unknown_policy | pull | core | 1 | api | n/a |
| abstain_unknown_policy | pull | medium | 0 | api | n/a |
| abstain_unknown_policy | pull | medium | 1 | api | n/a |
| abstain_unknown_policy | push_breadcrumb | core | 0 | api | n/a |
| abstain_unknown_policy | push_breadcrumb | core | 1 | api | n/a |
| abstain_unknown_policy | push_breadcrumb | medium | 0 | api | n/a |
| abstain_unknown_policy | push_breadcrumb | medium | 1 | api | n/a |
| abstain_unknown_policy | push_breadcrumb_pull | core | 0 | api | n/a |
| abstain_unknown_policy | push_breadcrumb_pull | core | 1 | api | n/a |
| abstain_unknown_policy | push_breadcrumb_pull | medium | 0 | api | n/a |
| abstain_unknown_policy | push_breadcrumb_pull | medium | 1 | api | n/a |
| abstain_unknown_policy | push_pages_upper_bound | core | 0 | api | n/a |
| abstain_unknown_policy | push_pages_upper_bound | core | 1 | api | n/a |
| abstain_unknown_policy | push_pages_upper_bound | medium | 0 | api | n/a |
| abstain_unknown_policy | push_pages_upper_bound | medium | 1 | api | n/a |
| acme_billing_exception | native_grep | core | 0 | api | no |
| acme_billing_exception | native_grep | core | 1 | api | no |
| acme_billing_exception | native_grep | medium | 0 | api | no |
| acme_billing_exception | native_grep | medium | 1 | api | no |
| acme_billing_exception | native_index | core | 0 | api | no |
| acme_billing_exception | native_index | core | 1 | api | no |
| acme_billing_exception | native_index | medium | 0 | api | no |
| acme_billing_exception | native_index | medium | 1 | api | no |
| acme_billing_exception | none | core | 0 | api | no |
| acme_billing_exception | none | core | 1 | api | no |
| acme_billing_exception | none | medium | 0 | api | no |
| acme_billing_exception | none | medium | 1 | api | no |
| acme_billing_exception | oracle | core | 0 | api | yes |
| acme_billing_exception | oracle | core | 1 | api | yes |
| acme_billing_exception | oracle | medium | 0 | api | yes |
| acme_billing_exception | oracle | medium | 1 | api | yes |
| acme_billing_exception | pull | core | 0 | api | yes |
| acme_billing_exception | pull | core | 1 | api | yes |
| acme_billing_exception | pull | medium | 0 | api | yes |
| acme_billing_exception | pull | medium | 1 | api | yes |
| acme_billing_exception | push_breadcrumb | core | 0 | api | no |
| acme_billing_exception | push_breadcrumb | core | 1 | api | no |
| acme_billing_exception | push_breadcrumb | medium | 0 | api | no |
| acme_billing_exception | push_breadcrumb | medium | 1 | api | no |
| acme_billing_exception | push_breadcrumb_pull | core | 0 | api | yes |
| acme_billing_exception | push_breadcrumb_pull | core | 1 | api | yes |
| acme_billing_exception | push_breadcrumb_pull | medium | 0 | api | yes |
| acme_billing_exception | push_breadcrumb_pull | medium | 1 | api | yes |
| acme_billing_exception | push_pages_upper_bound | core | 0 | api | yes |
| acme_billing_exception | push_pages_upper_bound | core | 1 | api | yes |
| acme_billing_exception | push_pages_upper_bound | medium | 0 | api | yes |
| acme_billing_exception | push_pages_upper_bound | medium | 1 | api | yes |
| aggregation_delivery_roles | native_grep | core | 0 | api | yes |
| aggregation_delivery_roles | native_grep | core | 1 | api | yes |
| aggregation_delivery_roles | native_grep | medium | 0 | api | no |
| aggregation_delivery_roles | native_grep | medium | 1 | api | no |
| aggregation_delivery_roles | native_index | core | 0 | api | no |
| aggregation_delivery_roles | native_index | core | 1 | api | no |
| aggregation_delivery_roles | native_index | medium | 0 | api | no |
| aggregation_delivery_roles | native_index | medium | 1 | api | no |
| aggregation_delivery_roles | none | core | 0 | api | no |
| aggregation_delivery_roles | none | core | 1 | api | no |
| aggregation_delivery_roles | none | medium | 0 | api | no |
| aggregation_delivery_roles | none | medium | 1 | api | no |
| aggregation_delivery_roles | oracle | core | 0 | api | yes |
| aggregation_delivery_roles | oracle | core | 1 | api | yes |
| aggregation_delivery_roles | oracle | medium | 0 | api | yes |
| aggregation_delivery_roles | oracle | medium | 1 | api | yes |
| aggregation_delivery_roles | pull | core | 0 | api | no |
| aggregation_delivery_roles | pull | core | 1 | api | no |
| aggregation_delivery_roles | pull | medium | 0 | api | no |
| aggregation_delivery_roles | pull | medium | 1 | api | yes |
| aggregation_delivery_roles | push_breadcrumb | core | 0 | api | no |
| aggregation_delivery_roles | push_breadcrumb | core | 1 | api | no |
| aggregation_delivery_roles | push_breadcrumb | medium | 0 | api | no |
| aggregation_delivery_roles | push_breadcrumb | medium | 1 | api | no |
| aggregation_delivery_roles | push_breadcrumb_pull | core | 0 | api | no |
| aggregation_delivery_roles | push_breadcrumb_pull | core | 1 | api | no |
| aggregation_delivery_roles | push_breadcrumb_pull | medium | 0 | api | yes |
| aggregation_delivery_roles | push_breadcrumb_pull | medium | 1 | api | no |
| aggregation_delivery_roles | push_pages_upper_bound | core | 0 | api | no |
| aggregation_delivery_roles | push_pages_upper_bound | core | 1 | api | no |
| aggregation_delivery_roles | push_pages_upper_bound | medium | 0 | api | no |
| aggregation_delivery_roles | push_pages_upper_bound | medium | 1 | api | no |
| aggregation_inhouse_tools | native_grep | core | 0 | api | no |
| aggregation_inhouse_tools | native_grep | core | 1 | api | yes |
| aggregation_inhouse_tools | native_grep | medium | 0 | api | yes |
| aggregation_inhouse_tools | native_grep | medium | 1 | api | yes |
| aggregation_inhouse_tools | native_index | core | 0 | api | no |
| aggregation_inhouse_tools | native_index | core | 1 | api | no |
| aggregation_inhouse_tools | native_index | medium | 0 | api | yes |
| aggregation_inhouse_tools | native_index | medium | 1 | api | yes |
| aggregation_inhouse_tools | none | core | 0 | api | no |
| aggregation_inhouse_tools | none | core | 1 | api | no |
| aggregation_inhouse_tools | none | medium | 0 | api | no |
| aggregation_inhouse_tools | none | medium | 1 | api | no |
| aggregation_inhouse_tools | oracle | core | 0 | api | yes |
| aggregation_inhouse_tools | oracle | core | 1 | api | yes |
| aggregation_inhouse_tools | oracle | medium | 0 | api | yes |
| aggregation_inhouse_tools | oracle | medium | 1 | api | yes |
| aggregation_inhouse_tools | pull | core | 0 | api | no |
| aggregation_inhouse_tools | pull | core | 1 | api | yes |
| aggregation_inhouse_tools | pull | medium | 0 | api | no |
| aggregation_inhouse_tools | pull | medium | 1 | api | no |
| aggregation_inhouse_tools | push_breadcrumb | core | 0 | api | no |
| aggregation_inhouse_tools | push_breadcrumb | core | 1 | api | no |
| aggregation_inhouse_tools | push_breadcrumb | medium | 0 | api | no |
| aggregation_inhouse_tools | push_breadcrumb | medium | 1 | api | no |
| aggregation_inhouse_tools | push_breadcrumb_pull | core | 0 | api | no |
| aggregation_inhouse_tools | push_breadcrumb_pull | core | 1 | api | yes |
| aggregation_inhouse_tools | push_breadcrumb_pull | medium | 0 | api | yes |
| aggregation_inhouse_tools | push_breadcrumb_pull | medium | 1 | api | yes |
| aggregation_inhouse_tools | push_pages_upper_bound | core | 0 | api | no |
| aggregation_inhouse_tools | push_pages_upper_bound | core | 1 | api | no |
| aggregation_inhouse_tools | push_pages_upper_bound | medium | 0 | api | no |
| aggregation_inhouse_tools | push_pages_upper_bound | medium | 1 | api | no |
| aggregation_retainer_clients | native_grep | core | 0 | api | yes |
| aggregation_retainer_clients | native_grep | core | 1 | api | yes |
| aggregation_retainer_clients | native_grep | medium | 0 | api | no |
| aggregation_retainer_clients | native_grep | medium | 1 | api | no |
| aggregation_retainer_clients | native_index | core | 0 | api | no |
| aggregation_retainer_clients | native_index | core | 1 | api | no |
| aggregation_retainer_clients | native_index | medium | 0 | api | no |
| aggregation_retainer_clients | native_index | medium | 1 | api | no |
| aggregation_retainer_clients | none | core | 0 | api | no |
| aggregation_retainer_clients | none | core | 1 | api | no |
| aggregation_retainer_clients | none | medium | 0 | api | no |
| aggregation_retainer_clients | none | medium | 1 | api | no |
| aggregation_retainer_clients | oracle | core | 0 | api | yes |
| aggregation_retainer_clients | oracle | core | 1 | api | yes |
| aggregation_retainer_clients | oracle | medium | 0 | api | yes |
| aggregation_retainer_clients | oracle | medium | 1 | api | yes |
| aggregation_retainer_clients | pull | core | 0 | api | no |
| aggregation_retainer_clients | pull | core | 1 | api | no |
| aggregation_retainer_clients | pull | medium | 0 | api | no |
| aggregation_retainer_clients | pull | medium | 1 | api | no |
| aggregation_retainer_clients | push_breadcrumb | core | 0 | api | no |
| aggregation_retainer_clients | push_breadcrumb | core | 1 | api | no |
| aggregation_retainer_clients | push_breadcrumb | medium | 0 | api | no |
| aggregation_retainer_clients | push_breadcrumb | medium | 1 | api | no |
| aggregation_retainer_clients | push_breadcrumb_pull | core | 0 | api | yes |
| aggregation_retainer_clients | push_breadcrumb_pull | core | 1 | api | no |
| aggregation_retainer_clients | push_breadcrumb_pull | medium | 0 | api | no |
| aggregation_retainer_clients | push_breadcrumb_pull | medium | 1 | api | yes |
| aggregation_retainer_clients | push_pages_upper_bound | core | 0 | api | no |
| aggregation_retainer_clients | push_pages_upper_bound | core | 1 | api | no |
| aggregation_retainer_clients | push_pages_upper_bound | medium | 0 | api | no |
| aggregation_retainer_clients | push_pages_upper_bound | medium | 1 | api | no |
| anchorline_retirement_rationale | native_grep | core | 0 | api | yes |
| anchorline_retirement_rationale | native_grep | core | 1 | api | yes |
| anchorline_retirement_rationale | native_grep | medium | 0 | api | yes |
| anchorline_retirement_rationale | native_grep | medium | 1 | api | yes |
| anchorline_retirement_rationale | native_index | core | 0 | api | no |
| anchorline_retirement_rationale | native_index | core | 1 | api | yes |
| anchorline_retirement_rationale | native_index | medium | 0 | api | yes |
| anchorline_retirement_rationale | native_index | medium | 1 | api | yes |
| anchorline_retirement_rationale | none | core | 0 | api | no |
| anchorline_retirement_rationale | none | core | 1 | api | no |
| anchorline_retirement_rationale | none | medium | 0 | api | no |
| anchorline_retirement_rationale | none | medium | 1 | api | no |
| anchorline_retirement_rationale | oracle | core | 0 | api | yes |
| anchorline_retirement_rationale | oracle | core | 1 | api | yes |
| anchorline_retirement_rationale | oracle | medium | 0 | api | yes |
| anchorline_retirement_rationale | oracle | medium | 1 | api | yes |
| anchorline_retirement_rationale | pull | core | 0 | api | yes |
| anchorline_retirement_rationale | pull | core | 1 | api | yes |
| anchorline_retirement_rationale | pull | medium | 0 | api | yes |
| anchorline_retirement_rationale | pull | medium | 1 | api | yes |
| anchorline_retirement_rationale | push_breadcrumb | core | 0 | api | no |
| anchorline_retirement_rationale | push_breadcrumb | core | 1 | api | no |
| anchorline_retirement_rationale | push_breadcrumb | medium | 0 | api | no |
| anchorline_retirement_rationale | push_breadcrumb | medium | 1 | api | no |
| anchorline_retirement_rationale | push_breadcrumb_pull | core | 0 | api | yes |
| anchorline_retirement_rationale | push_breadcrumb_pull | core | 1 | api | yes |
| anchorline_retirement_rationale | push_breadcrumb_pull | medium | 0 | api | yes |
| anchorline_retirement_rationale | push_breadcrumb_pull | medium | 1 | api | yes |
| anchorline_retirement_rationale | push_pages_upper_bound | core | 0 | api | no |
| anchorline_retirement_rationale | push_pages_upper_bound | core | 1 | api | no |
| anchorline_retirement_rationale | push_pages_upper_bound | medium | 0 | api | no |
| anchorline_retirement_rationale | push_pages_upper_bound | medium | 1 | api | no |
| bluewater_terms | native_grep | core | 0 | api | yes |
| bluewater_terms | native_grep | core | 1 | api | yes |
| bluewater_terms | native_grep | medium | 0 | api | yes |
| bluewater_terms | native_grep | medium | 1 | api | yes |
| bluewater_terms | native_index | core | 0 | api | yes |
| bluewater_terms | native_index | core | 1 | api | yes |
| bluewater_terms | native_index | medium | 0 | api | yes |
| bluewater_terms | native_index | medium | 1 | api | yes |
| bluewater_terms | none | core | 0 | api | no |
| bluewater_terms | none | core | 1 | api | no |
| bluewater_terms | none | medium | 0 | api | no |
| bluewater_terms | none | medium | 1 | api | no |
| bluewater_terms | oracle | core | 0 | api | yes |
| bluewater_terms | oracle | core | 1 | api | yes |
| bluewater_terms | oracle | medium | 0 | api | yes |
| bluewater_terms | oracle | medium | 1 | api | yes |
| bluewater_terms | pull | core | 0 | api | yes |
| bluewater_terms | pull | core | 1 | api | yes |
| bluewater_terms | pull | medium | 0 | api | yes |
| bluewater_terms | pull | medium | 1 | api | yes |
| bluewater_terms | push_breadcrumb | core | 0 | api | no |
| bluewater_terms | push_breadcrumb | core | 1 | api | no |
| bluewater_terms | push_breadcrumb | medium | 0 | api | no |
| bluewater_terms | push_breadcrumb | medium | 1 | api | no |
| bluewater_terms | push_breadcrumb_pull | core | 0 | api | yes |
| bluewater_terms | push_breadcrumb_pull | core | 1 | api | yes |
| bluewater_terms | push_breadcrumb_pull | medium | 0 | api | yes |
| bluewater_terms | push_breadcrumb_pull | medium | 1 | api | yes |
| bluewater_terms | push_pages_upper_bound | core | 0 | api | yes |
| bluewater_terms | push_pages_upper_bound | core | 1 | api | yes |
| bluewater_terms | push_pages_upper_bound | medium | 0 | api | yes |
| bluewater_terms | push_pages_upper_bound | medium | 1 | api | yes |
| bramfield_retainer_renewal | native_grep | core | 0 | api | yes |
| bramfield_retainer_renewal | native_grep | core | 1 | api | yes |
| bramfield_retainer_renewal | native_grep | medium | 0 | api | yes |
| bramfield_retainer_renewal | native_grep | medium | 1 | api | yes |
| bramfield_retainer_renewal | native_index | core | 0 | api | yes |
| bramfield_retainer_renewal | native_index | core | 1 | api | yes |
| bramfield_retainer_renewal | native_index | medium | 0 | api | yes |
| bramfield_retainer_renewal | native_index | medium | 1 | api | yes |
| bramfield_retainer_renewal | none | core | 0 | api | no |
| bramfield_retainer_renewal | none | core | 1 | api | no |
| bramfield_retainer_renewal | none | medium | 0 | api | no |
| bramfield_retainer_renewal | none | medium | 1 | api | no |
| bramfield_retainer_renewal | oracle | core | 0 | api | yes |
| bramfield_retainer_renewal | oracle | core | 1 | api | yes |
| bramfield_retainer_renewal | oracle | medium | 0 | api | yes |
| bramfield_retainer_renewal | oracle | medium | 1 | api | yes |
| bramfield_retainer_renewal | pull | core | 0 | api | yes |
| bramfield_retainer_renewal | pull | core | 1 | api | yes |
| bramfield_retainer_renewal | pull | medium | 0 | api | yes |
| bramfield_retainer_renewal | pull | medium | 1 | api | yes |
| bramfield_retainer_renewal | push_breadcrumb | core | 0 | api | no |
| bramfield_retainer_renewal | push_breadcrumb | core | 1 | api | no |
| bramfield_retainer_renewal | push_breadcrumb | medium | 0 | api | no |
| bramfield_retainer_renewal | push_breadcrumb | medium | 1 | api | no |
| bramfield_retainer_renewal | push_breadcrumb_pull | core | 0 | api | yes |
| bramfield_retainer_renewal | push_breadcrumb_pull | core | 1 | api | yes |
| bramfield_retainer_renewal | push_breadcrumb_pull | medium | 0 | api | yes |
| bramfield_retainer_renewal | push_breadcrumb_pull | medium | 1 | api | yes |
| bramfield_retainer_renewal | push_pages_upper_bound | core | 0 | api | no |
| bramfield_retainer_renewal | push_pages_upper_bound | core | 1 | api | no |
| bramfield_retainer_renewal | push_pages_upper_bound | medium | 0 | api | no |
| bramfield_retainer_renewal | push_pages_upper_bound | medium | 1 | api | no |
| budget_threshold_current | native_grep | core | 0 | api | yes |
| budget_threshold_current | native_grep | core | 1 | api | yes |
| budget_threshold_current | native_grep | medium | 0 | api | yes |
| budget_threshold_current | native_grep | medium | 1 | api | yes |
| budget_threshold_current | native_index | core | 0 | api | no |
| budget_threshold_current | native_index | core | 1 | api | no |
| budget_threshold_current | native_index | medium | 0 | api | yes |
| budget_threshold_current | native_index | medium | 1 | api | no |
| budget_threshold_current | none | core | 0 | api | no |
| budget_threshold_current | none | core | 1 | api | no |
| budget_threshold_current | none | medium | 0 | api | no |
| budget_threshold_current | none | medium | 1 | api | no |
| budget_threshold_current | oracle | core | 0 | api | yes |
| budget_threshold_current | oracle | core | 1 | api | yes |
| budget_threshold_current | oracle | medium | 0 | api | yes |
| budget_threshold_current | oracle | medium | 1 | api | yes |
| budget_threshold_current | pull | core | 0 | api | yes |
| budget_threshold_current | pull | core | 1 | api | yes |
| budget_threshold_current | pull | medium | 0 | api | yes |
| budget_threshold_current | pull | medium | 1 | api | no |
| budget_threshold_current | push_breadcrumb | core | 0 | api | no |
| budget_threshold_current | push_breadcrumb | core | 1 | api | no |
| budget_threshold_current | push_breadcrumb | medium | 0 | api | no |
| budget_threshold_current | push_breadcrumb | medium | 1 | api | no |
| budget_threshold_current | push_breadcrumb_pull | core | 0 | api | yes |
| budget_threshold_current | push_breadcrumb_pull | core | 1 | api | yes |
| budget_threshold_current | push_breadcrumb_pull | medium | 0 | api | no |
| budget_threshold_current | push_breadcrumb_pull | medium | 1 | api | yes |
| budget_threshold_current | push_pages_upper_bound | core | 0 | api | yes |
| budget_threshold_current | push_pages_upper_bound | core | 1 | api | yes |
| budget_threshold_current | push_pages_upper_bound | medium | 0 | api | yes |
| budget_threshold_current | push_pages_upper_bound | medium | 1 | api | yes |
| callum_drews_last_contact | native_grep | core | 0 | api | yes |
| callum_drews_last_contact | native_grep | core | 1 | api | yes |
| callum_drews_last_contact | native_grep | medium | 0 | api | yes |
| callum_drews_last_contact | native_grep | medium | 1 | api | yes |
| callum_drews_last_contact | native_index | core | 0 | api | yes |
| callum_drews_last_contact | native_index | core | 1 | api | no |
| callum_drews_last_contact | native_index | medium | 0 | api | yes |
| callum_drews_last_contact | native_index | medium | 1 | api | no |
| callum_drews_last_contact | none | core | 0 | api | no |
| callum_drews_last_contact | none | core | 1 | api | no |
| callum_drews_last_contact | none | medium | 0 | api | no |
| callum_drews_last_contact | none | medium | 1 | api | no |
| callum_drews_last_contact | oracle | core | 0 | api | yes |
| callum_drews_last_contact | oracle | core | 1 | api | yes |
| callum_drews_last_contact | oracle | medium | 0 | api | yes |
| callum_drews_last_contact | oracle | medium | 1 | api | yes |
| callum_drews_last_contact | pull | core | 0 | api | yes |
| callum_drews_last_contact | pull | core | 1 | api | yes |
| callum_drews_last_contact | pull | medium | 0 | api | yes |
| callum_drews_last_contact | pull | medium | 1 | api | yes |
| callum_drews_last_contact | push_breadcrumb | core | 0 | api | no |
| callum_drews_last_contact | push_breadcrumb | core | 1 | api | no |
| callum_drews_last_contact | push_breadcrumb | medium | 0 | api | no |
| callum_drews_last_contact | push_breadcrumb | medium | 1 | api | no |
| callum_drews_last_contact | push_breadcrumb_pull | core | 0 | api | yes |
| callum_drews_last_contact | push_breadcrumb_pull | core | 1 | api | yes |
| callum_drews_last_contact | push_breadcrumb_pull | medium | 0 | api | yes |
| callum_drews_last_contact | push_breadcrumb_pull | medium | 1 | api | yes |
| callum_drews_last_contact | push_pages_upper_bound | core | 0 | api | yes |
| callum_drews_last_contact | push_pages_upper_bound | core | 1 | api | yes |
| callum_drews_last_contact | push_pages_upper_bound | medium | 0 | api | yes |
| callum_drews_last_contact | push_pages_upper_bound | medium | 1 | api | yes |
| confidentiality_rule | native_grep | core | 0 | api | yes |
| confidentiality_rule | native_grep | core | 1 | api | yes |
| confidentiality_rule | native_grep | medium | 0 | api | yes |
| confidentiality_rule | native_grep | medium | 1 | api | yes |
| confidentiality_rule | native_index | core | 0 | api | yes |
| confidentiality_rule | native_index | core | 1 | api | yes |
| confidentiality_rule | native_index | medium | 0 | api | yes |
| confidentiality_rule | native_index | medium | 1 | api | yes |
| confidentiality_rule | none | core | 0 | api | no |
| confidentiality_rule | none | core | 1 | api | no |
| confidentiality_rule | none | medium | 0 | api | no |
| confidentiality_rule | none | medium | 1 | api | no |
| confidentiality_rule | oracle | core | 0 | api | yes |
| confidentiality_rule | oracle | core | 1 | api | yes |
| confidentiality_rule | oracle | medium | 0 | api | yes |
| confidentiality_rule | oracle | medium | 1 | api | yes |
| confidentiality_rule | pull | core | 0 | api | yes |
| confidentiality_rule | pull | core | 1 | api | yes |
| confidentiality_rule | pull | medium | 0 | api | yes |
| confidentiality_rule | pull | medium | 1 | api | yes |
| confidentiality_rule | push_breadcrumb | core | 0 | api | no |
| confidentiality_rule | push_breadcrumb | core | 1 | api | no |
| confidentiality_rule | push_breadcrumb | medium | 0 | api | no |
| confidentiality_rule | push_breadcrumb | medium | 1 | api | no |
| confidentiality_rule | push_breadcrumb_pull | core | 0 | api | yes |
| confidentiality_rule | push_breadcrumb_pull | core | 1 | api | yes |
| confidentiality_rule | push_breadcrumb_pull | medium | 0 | api | yes |
| confidentiality_rule | push_breadcrumb_pull | medium | 1 | api | yes |
| confidentiality_rule | push_pages_upper_bound | core | 0 | api | yes |
| confidentiality_rule | push_pages_upper_bound | core | 1 | api | yes |
| confidentiality_rule | push_pages_upper_bound | medium | 0 | api | yes |
| confidentiality_rule | push_pages_upper_bound | medium | 1 | api | yes |
| corvale_holdings_history | native_grep | core | 0 | api | yes |
| corvale_holdings_history | native_grep | core | 1 | api | yes |
| corvale_holdings_history | native_grep | medium | 0 | api | no |
| corvale_holdings_history | native_grep | medium | 1 | api | no |
| corvale_holdings_history | native_index | core | 0 | api | yes |
| corvale_holdings_history | native_index | core | 1 | api | yes |
| corvale_holdings_history | native_index | medium | 0 | api | yes |
| corvale_holdings_history | native_index | medium | 1 | api | yes |
| corvale_holdings_history | none | core | 0 | api | no |
| corvale_holdings_history | none | core | 1 | api | no |
| corvale_holdings_history | none | medium | 0 | api | no |
| corvale_holdings_history | none | medium | 1 | api | no |
| corvale_holdings_history | oracle | core | 0 | api | yes |
| corvale_holdings_history | oracle | core | 1 | api | yes |
| corvale_holdings_history | oracle | medium | 0 | api | yes |
| corvale_holdings_history | oracle | medium | 1 | api | yes |
| corvale_holdings_history | pull | core | 0 | api | yes |
| corvale_holdings_history | pull | core | 1 | api | yes |
| corvale_holdings_history | pull | medium | 0 | api | yes |
| corvale_holdings_history | pull | medium | 1 | api | yes |
| corvale_holdings_history | push_breadcrumb | core | 0 | api | no |
| corvale_holdings_history | push_breadcrumb | core | 1 | api | no |
| corvale_holdings_history | push_breadcrumb | medium | 0 | api | no |
| corvale_holdings_history | push_breadcrumb | medium | 1 | api | no |
| corvale_holdings_history | push_breadcrumb_pull | core | 0 | api | yes |
| corvale_holdings_history | push_breadcrumb_pull | core | 1 | api | yes |
| corvale_holdings_history | push_breadcrumb_pull | medium | 0 | api | yes |
| corvale_holdings_history | push_breadcrumb_pull | medium | 1 | api | yes |
| corvale_holdings_history | push_pages_upper_bound | core | 0 | api | no |
| corvale_holdings_history | push_pages_upper_bound | core | 1 | api | no |
| corvale_holdings_history | push_pages_upper_bound | medium | 0 | api | no |
| corvale_holdings_history | push_pages_upper_bound | medium | 1 | api | no |
| driftgate_migration_funding | native_grep | core | 0 | api | no |
| driftgate_migration_funding | native_grep | core | 1 | api | no |
| driftgate_migration_funding | native_grep | medium | 0 | api | no |
| driftgate_migration_funding | native_grep | medium | 1 | api | no |
| driftgate_migration_funding | native_index | core | 0 | api | no |
| driftgate_migration_funding | native_index | core | 1 | api | no |
| driftgate_migration_funding | native_index | medium | 0 | api | no |
| driftgate_migration_funding | native_index | medium | 1 | api | no |
| driftgate_migration_funding | none | core | 0 | api | no |
| driftgate_migration_funding | none | core | 1 | api | no |
| driftgate_migration_funding | none | medium | 0 | api | no |
| driftgate_migration_funding | none | medium | 1 | api | no |
| driftgate_migration_funding | oracle | core | 0 | api | yes |
| driftgate_migration_funding | oracle | core | 1 | api | yes |
| driftgate_migration_funding | oracle | medium | 0 | api | yes |
| driftgate_migration_funding | oracle | medium | 1 | api | yes |
| driftgate_migration_funding | pull | core | 0 | api | no |
| driftgate_migration_funding | pull | core | 1 | api | no |
| driftgate_migration_funding | pull | medium | 0 | api | no |
| driftgate_migration_funding | pull | medium | 1 | api | no |
| driftgate_migration_funding | push_breadcrumb | core | 0 | api | no |
| driftgate_migration_funding | push_breadcrumb | core | 1 | api | no |
| driftgate_migration_funding | push_breadcrumb | medium | 0 | api | no |
| driftgate_migration_funding | push_breadcrumb | medium | 1 | api | no |
| driftgate_migration_funding | push_breadcrumb_pull | core | 0 | api | no |
| driftgate_migration_funding | push_breadcrumb_pull | core | 1 | api | no |
| driftgate_migration_funding | push_breadcrumb_pull | medium | 0 | api | no |
| driftgate_migration_funding | push_breadcrumb_pull | medium | 1 | api | no |
| driftgate_migration_funding | push_pages_upper_bound | core | 0 | api | no |
| driftgate_migration_funding | push_pages_upper_bound | core | 1 | api | no |
| driftgate_migration_funding | push_pages_upper_bound | medium | 0 | api | no |
| driftgate_migration_funding | push_pages_upper_bound | medium | 1 | api | no |
| dual_signoff_threshold | native_grep | core | 0 | api | no |
| dual_signoff_threshold | native_grep | core | 1 | api | no |
| dual_signoff_threshold | native_grep | medium | 0 | api | no |
| dual_signoff_threshold | native_grep | medium | 1 | api | no |
| dual_signoff_threshold | native_index | core | 0 | api | no |
| dual_signoff_threshold | native_index | core | 1 | api | no |
| dual_signoff_threshold | native_index | medium | 0 | api | no |
| dual_signoff_threshold | native_index | medium | 1 | api | no |
| dual_signoff_threshold | none | core | 0 | api | no |
| dual_signoff_threshold | none | core | 1 | api | no |
| dual_signoff_threshold | none | medium | 0 | api | no |
| dual_signoff_threshold | none | medium | 1 | api | no |
| dual_signoff_threshold | oracle | core | 0 | api | yes |
| dual_signoff_threshold | oracle | core | 1 | api | no |
| dual_signoff_threshold | oracle | medium | 0 | api | yes |
| dual_signoff_threshold | oracle | medium | 1 | api | no |
| dual_signoff_threshold | pull | core | 0 | api | no |
| dual_signoff_threshold | pull | core | 1 | api | no |
| dual_signoff_threshold | pull | medium | 0 | api | no |
| dual_signoff_threshold | pull | medium | 1 | api | no |
| dual_signoff_threshold | push_breadcrumb | core | 0 | api | no |
| dual_signoff_threshold | push_breadcrumb | core | 1 | api | no |
| dual_signoff_threshold | push_breadcrumb | medium | 0 | api | no |
| dual_signoff_threshold | push_breadcrumb | medium | 1 | api | no |
| dual_signoff_threshold | push_breadcrumb_pull | core | 0 | api | no |
| dual_signoff_threshold | push_breadcrumb_pull | core | 1 | api | no |
| dual_signoff_threshold | push_breadcrumb_pull | medium | 0 | api | no |
| dual_signoff_threshold | push_breadcrumb_pull | medium | 1 | api | no |
| dual_signoff_threshold | push_pages_upper_bound | core | 0 | api | no |
| dual_signoff_threshold | push_pages_upper_bound | core | 1 | api | no |
| dual_signoff_threshold | push_pages_upper_bound | medium | 0 | api | no |
| dual_signoff_threshold | push_pages_upper_bound | medium | 1 | api | no |
| expense_approval_limit_manager | native_grep | core | 0 | api | yes |
| expense_approval_limit_manager | native_grep | core | 1 | api | yes |
| expense_approval_limit_manager | native_grep | medium | 0 | api | yes |
| expense_approval_limit_manager | native_grep | medium | 1 | api | yes |
| expense_approval_limit_manager | native_index | core | 0 | api | yes |
| expense_approval_limit_manager | native_index | core | 1 | api | yes |
| expense_approval_limit_manager | native_index | medium | 0 | api | yes |
| expense_approval_limit_manager | native_index | medium | 1 | api | yes |
| expense_approval_limit_manager | none | core | 0 | api | no |
| expense_approval_limit_manager | none | core | 1 | api | no |
| expense_approval_limit_manager | none | medium | 0 | api | no |
| expense_approval_limit_manager | none | medium | 1 | api | no |
| expense_approval_limit_manager | oracle | core | 0 | api | no |
| expense_approval_limit_manager | oracle | core | 1 | api | no |
| expense_approval_limit_manager | oracle | medium | 0 | api | no |
| expense_approval_limit_manager | oracle | medium | 1 | api | no |
| expense_approval_limit_manager | pull | core | 0 | api | yes |
| expense_approval_limit_manager | pull | core | 1 | api | yes |
| expense_approval_limit_manager | pull | medium | 0 | api | yes |
| expense_approval_limit_manager | pull | medium | 1 | api | yes |
| expense_approval_limit_manager | push_breadcrumb | core | 0 | api | no |
| expense_approval_limit_manager | push_breadcrumb | core | 1 | api | no |
| expense_approval_limit_manager | push_breadcrumb | medium | 0 | api | no |
| expense_approval_limit_manager | push_breadcrumb | medium | 1 | api | no |
| expense_approval_limit_manager | push_breadcrumb_pull | core | 0 | api | yes |
| expense_approval_limit_manager | push_breadcrumb_pull | core | 1 | api | yes |
| expense_approval_limit_manager | push_breadcrumb_pull | medium | 0 | api | yes |
| expense_approval_limit_manager | push_breadcrumb_pull | medium | 1 | api | yes |
| expense_approval_limit_manager | push_pages_upper_bound | core | 0 | api | no |
| expense_approval_limit_manager | push_pages_upper_bound | core | 1 | api | no |
| expense_approval_limit_manager | push_pages_upper_bound | medium | 0 | api | no |
| expense_approval_limit_manager | push_pages_upper_bound | medium | 1 | api | no |
| fenwick_relationship_history | native_grep | core | 0 | api | no |
| fenwick_relationship_history | native_grep | core | 1 | api | yes |
| fenwick_relationship_history | native_grep | medium | 0 | api | no |
| fenwick_relationship_history | native_grep | medium | 1 | api | no |
| fenwick_relationship_history | native_index | core | 0 | api | yes |
| fenwick_relationship_history | native_index | core | 1 | api | yes |
| fenwick_relationship_history | native_index | medium | 0 | api | yes |
| fenwick_relationship_history | native_index | medium | 1 | api | yes |
| fenwick_relationship_history | none | core | 0 | api | no |
| fenwick_relationship_history | none | core | 1 | api | no |
| fenwick_relationship_history | none | medium | 0 | api | no |
| fenwick_relationship_history | none | medium | 1 | api | no |
| fenwick_relationship_history | oracle | core | 0 | api | yes |
| fenwick_relationship_history | oracle | core | 1 | api | yes |
| fenwick_relationship_history | oracle | medium | 0 | api | yes |
| fenwick_relationship_history | oracle | medium | 1 | api | yes |
| fenwick_relationship_history | pull | core | 0 | api | yes |
| fenwick_relationship_history | pull | core | 1 | api | yes |
| fenwick_relationship_history | pull | medium | 0 | api | yes |
| fenwick_relationship_history | pull | medium | 1 | api | yes |
| fenwick_relationship_history | push_breadcrumb | core | 0 | api | no |
| fenwick_relationship_history | push_breadcrumb | core | 1 | api | no |
| fenwick_relationship_history | push_breadcrumb | medium | 0 | api | no |
| fenwick_relationship_history | push_breadcrumb | medium | 1 | api | no |
| fenwick_relationship_history | push_breadcrumb_pull | core | 0 | api | yes |
| fenwick_relationship_history | push_breadcrumb_pull | core | 1 | api | yes |
| fenwick_relationship_history | push_breadcrumb_pull | medium | 0 | api | yes |
| fenwick_relationship_history | push_breadcrumb_pull | medium | 1 | api | yes |
| fenwick_relationship_history | push_pages_upper_bound | core | 0 | api | no |
| fenwick_relationship_history | push_pages_upper_bound | core | 1 | api | no |
| fenwick_relationship_history | push_pages_upper_bound | medium | 0 | api | no |
| fenwick_relationship_history | push_pages_upper_bound | medium | 1 | api | no |
| former_client_not_current | native_grep | core | 0 | api | yes |
| former_client_not_current | native_grep | core | 1 | api | yes |
| former_client_not_current | native_grep | medium | 0 | api | yes |
| former_client_not_current | native_grep | medium | 1 | api | yes |
| former_client_not_current | native_index | core | 0 | api | no |
| former_client_not_current | native_index | core | 1 | api | no |
| former_client_not_current | native_index | medium | 0 | api | no |
| former_client_not_current | native_index | medium | 1 | api | no |
| former_client_not_current | none | core | 0 | api | no |
| former_client_not_current | none | core | 1 | api | no |
| former_client_not_current | none | medium | 0 | api | no |
| former_client_not_current | none | medium | 1 | api | no |
| former_client_not_current | oracle | core | 0 | api | yes |
| former_client_not_current | oracle | core | 1 | api | yes |
| former_client_not_current | oracle | medium | 0 | api | yes |
| former_client_not_current | oracle | medium | 1 | api | yes |
| former_client_not_current | pull | core | 0 | api | yes |
| former_client_not_current | pull | core | 1 | api | no |
| former_client_not_current | pull | medium | 0 | api | no |
| former_client_not_current | pull | medium | 1 | api | no |
| former_client_not_current | push_breadcrumb | core | 0 | api | no |
| former_client_not_current | push_breadcrumb | core | 1 | api | no |
| former_client_not_current | push_breadcrumb | medium | 0 | api | no |
| former_client_not_current | push_breadcrumb | medium | 1 | api | no |
| former_client_not_current | push_breadcrumb_pull | core | 0 | api | yes |
| former_client_not_current | push_breadcrumb_pull | core | 1 | api | yes |
| former_client_not_current | push_breadcrumb_pull | medium | 0 | api | yes |
| former_client_not_current | push_breadcrumb_pull | medium | 1 | api | yes |
| former_client_not_current | push_pages_upper_bound | core | 0 | api | yes |
| former_client_not_current | push_pages_upper_bound | core | 1 | api | yes |
| former_client_not_current | push_pages_upper_bound | medium | 0 | api | no |
| former_client_not_current | push_pages_upper_bound | medium | 1 | api | no |
| gilcrest_vendor_decision | native_grep | core | 0 | api | no |
| gilcrest_vendor_decision | native_grep | core | 1 | api | no |
| gilcrest_vendor_decision | native_grep | medium | 0 | api | no |
| gilcrest_vendor_decision | native_grep | medium | 1 | api | no |
| gilcrest_vendor_decision | native_index | core | 0 | api | yes |
| gilcrest_vendor_decision | native_index | core | 1 | api | yes |
| gilcrest_vendor_decision | native_index | medium | 0 | api | yes |
| gilcrest_vendor_decision | native_index | medium | 1 | api | yes |
| gilcrest_vendor_decision | none | core | 0 | api | no |
| gilcrest_vendor_decision | none | core | 1 | api | no |
| gilcrest_vendor_decision | none | medium | 0 | api | no |
| gilcrest_vendor_decision | none | medium | 1 | api | no |
| gilcrest_vendor_decision | oracle | core | 0 | api | yes |
| gilcrest_vendor_decision | oracle | core | 1 | api | yes |
| gilcrest_vendor_decision | oracle | medium | 0 | api | yes |
| gilcrest_vendor_decision | oracle | medium | 1 | api | yes |
| gilcrest_vendor_decision | pull | core | 0 | api | yes |
| gilcrest_vendor_decision | pull | core | 1 | api | yes |
| gilcrest_vendor_decision | pull | medium | 0 | api | yes |
| gilcrest_vendor_decision | pull | medium | 1 | api | yes |
| gilcrest_vendor_decision | push_breadcrumb | core | 0 | api | no |
| gilcrest_vendor_decision | push_breadcrumb | core | 1 | api | no |
| gilcrest_vendor_decision | push_breadcrumb | medium | 0 | api | no |
| gilcrest_vendor_decision | push_breadcrumb | medium | 1 | api | no |
| gilcrest_vendor_decision | push_breadcrumb_pull | core | 0 | api | no |
| gilcrest_vendor_decision | push_breadcrumb_pull | core | 1 | api | no |
| gilcrest_vendor_decision | push_breadcrumb_pull | medium | 0 | api | yes |
| gilcrest_vendor_decision | push_breadcrumb_pull | medium | 1 | api | yes |
| gilcrest_vendor_decision | push_pages_upper_bound | core | 0 | api | no |
| gilcrest_vendor_decision | push_pages_upper_bound | core | 1 | api | no |
| gilcrest_vendor_decision | push_pages_upper_bound | medium | 0 | api | no |
| gilcrest_vendor_decision | push_pages_upper_bound | medium | 1 | api | no |
| given_name_is_ambiguous | native_grep | core | 0 | api | yes |
| given_name_is_ambiguous | native_grep | core | 1 | api | no |
| given_name_is_ambiguous | native_grep | medium | 0 | api | yes |
| given_name_is_ambiguous | native_grep | medium | 1 | api | no |
| given_name_is_ambiguous | native_index | core | 0 | api | no |
| given_name_is_ambiguous | native_index | core | 1 | api | yes |
| given_name_is_ambiguous | native_index | medium | 0 | api | no |
| given_name_is_ambiguous | native_index | medium | 1 | api | no |
| given_name_is_ambiguous | none | core | 0 | api | no |
| given_name_is_ambiguous | none | core | 1 | api | no |
| given_name_is_ambiguous | none | medium | 0 | api | no |
| given_name_is_ambiguous | none | medium | 1 | api | no |
| given_name_is_ambiguous | oracle | core | 0 | api | yes |
| given_name_is_ambiguous | oracle | core | 1 | api | yes |
| given_name_is_ambiguous | oracle | medium | 0 | api | yes |
| given_name_is_ambiguous | oracle | medium | 1 | api | yes |
| given_name_is_ambiguous | pull | core | 0 | api | yes |
| given_name_is_ambiguous | pull | core | 1 | api | yes |
| given_name_is_ambiguous | pull | medium | 0 | api | yes |
| given_name_is_ambiguous | pull | medium | 1 | api | yes |
| given_name_is_ambiguous | push_breadcrumb | core | 0 | api | no |
| given_name_is_ambiguous | push_breadcrumb | core | 1 | api | no |
| given_name_is_ambiguous | push_breadcrumb | medium | 0 | api | no |
| given_name_is_ambiguous | push_breadcrumb | medium | 1 | api | no |
| given_name_is_ambiguous | push_breadcrumb_pull | core | 0 | api | yes |
| given_name_is_ambiguous | push_breadcrumb_pull | core | 1 | api | yes |
| given_name_is_ambiguous | push_breadcrumb_pull | medium | 0 | api | yes |
| given_name_is_ambiguous | push_breadcrumb_pull | medium | 1 | api | yes |
| given_name_is_ambiguous | push_pages_upper_bound | core | 0 | api | yes |
| given_name_is_ambiguous | push_pages_upper_bound | core | 1 | api | yes |
| given_name_is_ambiguous | push_pages_upper_bound | medium | 0 | api | yes |
| given_name_is_ambiguous | push_pages_upper_bound | medium | 1 | api | yes |
| invoicing_api_pagination_workaround | native_grep | core | 0 | api | no |
| invoicing_api_pagination_workaround | native_grep | core | 1 | api | no |
| invoicing_api_pagination_workaround | native_grep | medium | 0 | api | yes |
| invoicing_api_pagination_workaround | native_grep | medium | 1 | api | no |
| invoicing_api_pagination_workaround | native_index | core | 0 | api | no |
| invoicing_api_pagination_workaround | native_index | core | 1 | api | no |
| invoicing_api_pagination_workaround | native_index | medium | 0 | api | yes |
| invoicing_api_pagination_workaround | native_index | medium | 1 | api | no |
| invoicing_api_pagination_workaround | none | core | 0 | api | no |
| invoicing_api_pagination_workaround | none | core | 1 | api | no |
| invoicing_api_pagination_workaround | none | medium | 0 | api | no |
| invoicing_api_pagination_workaround | none | medium | 1 | api | no |
| invoicing_api_pagination_workaround | oracle | core | 0 | api | yes |
| invoicing_api_pagination_workaround | oracle | core | 1 | api | yes |
| invoicing_api_pagination_workaround | oracle | medium | 0 | api | yes |
| invoicing_api_pagination_workaround | oracle | medium | 1 | api | yes |
| invoicing_api_pagination_workaround | pull | core | 0 | api | yes |
| invoicing_api_pagination_workaround | pull | core | 1 | api | yes |
| invoicing_api_pagination_workaround | pull | medium | 0 | api | yes |
| invoicing_api_pagination_workaround | pull | medium | 1 | api | yes |
| invoicing_api_pagination_workaround | push_breadcrumb | core | 0 | api | no |
| invoicing_api_pagination_workaround | push_breadcrumb | core | 1 | api | no |
| invoicing_api_pagination_workaround | push_breadcrumb | medium | 0 | api | no |
| invoicing_api_pagination_workaround | push_breadcrumb | medium | 1 | api | no |
| invoicing_api_pagination_workaround | push_breadcrumb_pull | core | 0 | api | yes |
| invoicing_api_pagination_workaround | push_breadcrumb_pull | core | 1 | api | yes |
| invoicing_api_pagination_workaround | push_breadcrumb_pull | medium | 0 | api | yes |
| invoicing_api_pagination_workaround | push_breadcrumb_pull | medium | 1 | api | yes |
| invoicing_api_pagination_workaround | push_pages_upper_bound | core | 0 | api | yes |
| invoicing_api_pagination_workaround | push_pages_upper_bound | core | 1 | api | yes |
| invoicing_api_pagination_workaround | push_pages_upper_bound | medium | 0 | api | yes |
| invoicing_api_pagination_workaround | push_pages_upper_bound | medium | 1 | api | yes |
| keelbridge_programme_scope | native_grep | core | 0 | api | yes |
| keelbridge_programme_scope | native_grep | core | 1 | api | yes |
| keelbridge_programme_scope | native_grep | medium | 0 | api | yes |
| keelbridge_programme_scope | native_grep | medium | 1 | api | yes |
| keelbridge_programme_scope | native_index | core | 0 | api | yes |
| keelbridge_programme_scope | native_index | core | 1 | api | yes |
| keelbridge_programme_scope | native_index | medium | 0 | api | yes |
| keelbridge_programme_scope | native_index | medium | 1 | api | yes |
| keelbridge_programme_scope | none | core | 0 | api | no |
| keelbridge_programme_scope | none | core | 1 | api | no |
| keelbridge_programme_scope | none | medium | 0 | api | no |
| keelbridge_programme_scope | none | medium | 1 | api | no |
| keelbridge_programme_scope | oracle | core | 0 | api | yes |
| keelbridge_programme_scope | oracle | core | 1 | api | yes |
| keelbridge_programme_scope | oracle | medium | 0 | api | yes |
| keelbridge_programme_scope | oracle | medium | 1 | api | yes |
| keelbridge_programme_scope | pull | core | 0 | api | yes |
| keelbridge_programme_scope | pull | core | 1 | api | yes |
| keelbridge_programme_scope | pull | medium | 0 | api | yes |
| keelbridge_programme_scope | pull | medium | 1 | api | yes |
| keelbridge_programme_scope | push_breadcrumb | core | 0 | api | no |
| keelbridge_programme_scope | push_breadcrumb | core | 1 | api | no |
| keelbridge_programme_scope | push_breadcrumb | medium | 0 | api | no |
| keelbridge_programme_scope | push_breadcrumb | medium | 1 | api | no |
| keelbridge_programme_scope | push_breadcrumb_pull | core | 0 | api | no |
| keelbridge_programme_scope | push_breadcrumb_pull | core | 1 | api | no |
| keelbridge_programme_scope | push_breadcrumb_pull | medium | 0 | api | no |
| keelbridge_programme_scope | push_breadcrumb_pull | medium | 1 | api | no |
| keelbridge_programme_scope | push_pages_upper_bound | core | 0 | api | no |
| keelbridge_programme_scope | push_pages_upper_bound | core | 1 | api | no |
| keelbridge_programme_scope | push_pages_upper_bound | medium | 0 | api | no |
| keelbridge_programme_scope | push_pages_upper_bound | medium | 1 | api | no |
| lighthouse_migration_rollback | native_grep | core | 0 | api | yes |
| lighthouse_migration_rollback | native_grep | core | 1 | api | yes |
| lighthouse_migration_rollback | native_grep | medium | 0 | api | yes |
| lighthouse_migration_rollback | native_grep | medium | 1 | api | yes |
| lighthouse_migration_rollback | native_index | core | 0 | api | yes |
| lighthouse_migration_rollback | native_index | core | 1 | api | yes |
| lighthouse_migration_rollback | native_index | medium | 0 | api | yes |
| lighthouse_migration_rollback | native_index | medium | 1 | api | yes |
| lighthouse_migration_rollback | none | core | 0 | api | no |
| lighthouse_migration_rollback | none | core | 1 | api | no |
| lighthouse_migration_rollback | none | medium | 0 | api | no |
| lighthouse_migration_rollback | none | medium | 1 | api | no |
| lighthouse_migration_rollback | oracle | core | 0 | api | yes |
| lighthouse_migration_rollback | oracle | core | 1 | api | yes |
| lighthouse_migration_rollback | oracle | medium | 0 | api | yes |
| lighthouse_migration_rollback | oracle | medium | 1 | api | yes |
| lighthouse_migration_rollback | pull | core | 0 | api | yes |
| lighthouse_migration_rollback | pull | core | 1 | api | yes |
| lighthouse_migration_rollback | pull | medium | 0 | api | yes |
| lighthouse_migration_rollback | pull | medium | 1 | api | yes |
| lighthouse_migration_rollback | push_breadcrumb | core | 0 | api | no |
| lighthouse_migration_rollback | push_breadcrumb | core | 1 | api | no |
| lighthouse_migration_rollback | push_breadcrumb | medium | 0 | api | no |
| lighthouse_migration_rollback | push_breadcrumb | medium | 1 | api | no |
| lighthouse_migration_rollback | push_breadcrumb_pull | core | 0 | api | yes |
| lighthouse_migration_rollback | push_breadcrumb_pull | core | 1 | api | yes |
| lighthouse_migration_rollback | push_breadcrumb_pull | medium | 0 | api | yes |
| lighthouse_migration_rollback | push_breadcrumb_pull | medium | 1 | api | yes |
| lighthouse_migration_rollback | push_pages_upper_bound | core | 0 | api | no |
| lighthouse_migration_rollback | push_pages_upper_bound | core | 1 | api | no |
| lighthouse_migration_rollback | push_pages_upper_bound | medium | 0 | api | no |
| lighthouse_migration_rollback | push_pages_upper_bound | medium | 1 | api | no |
| mira_castellane_role | native_grep | core | 0 | api | no |
| mira_castellane_role | native_grep | core | 1 | api | no |
| mira_castellane_role | native_grep | medium | 0 | api | no |
| mira_castellane_role | native_grep | medium | 1 | api | no |
| mira_castellane_role | native_index | core | 0 | api | no |
| mira_castellane_role | native_index | core | 1 | api | no |
| mira_castellane_role | native_index | medium | 0 | api | no |
| mira_castellane_role | native_index | medium | 1 | api | no |
| mira_castellane_role | none | core | 0 | api | no |
| mira_castellane_role | none | core | 1 | api | no |
| mira_castellane_role | none | medium | 0 | api | no |
| mira_castellane_role | none | medium | 1 | api | no |
| mira_castellane_role | oracle | core | 0 | api | yes |
| mira_castellane_role | oracle | core | 1 | api | yes |
| mira_castellane_role | oracle | medium | 0 | api | yes |
| mira_castellane_role | oracle | medium | 1 | api | yes |
| mira_castellane_role | pull | core | 0 | api | no |
| mira_castellane_role | pull | core | 1 | api | no |
| mira_castellane_role | pull | medium | 0 | api | no |
| mira_castellane_role | pull | medium | 1 | api | no |
| mira_castellane_role | push_breadcrumb | core | 0 | api | no |
| mira_castellane_role | push_breadcrumb | core | 1 | api | no |
| mira_castellane_role | push_breadcrumb | medium | 0 | api | no |
| mira_castellane_role | push_breadcrumb | medium | 1 | api | no |
| mira_castellane_role | push_breadcrumb_pull | core | 0 | api | no |
| mira_castellane_role | push_breadcrumb_pull | core | 1 | api | no |
| mira_castellane_role | push_breadcrumb_pull | medium | 0 | api | no |
| mira_castellane_role | push_breadcrumb_pull | medium | 1 | api | yes |
| mira_castellane_role | push_pages_upper_bound | core | 0 | api | no |
| mira_castellane_role | push_pages_upper_bound | core | 1 | api | no |
| mira_castellane_role | push_pages_upper_bound | medium | 0 | api | yes |
| mira_castellane_role | push_pages_upper_bound | medium | 1 | api | yes |
| office_address_current | native_grep | core | 0 | api | yes |
| office_address_current | native_grep | core | 1 | api | yes |
| office_address_current | native_grep | medium | 0 | api | no |
| office_address_current | native_grep | medium | 1 | api | yes |
| office_address_current | native_index | core | 0 | api | yes |
| office_address_current | native_index | core | 1 | api | yes |
| office_address_current | native_index | medium | 0 | api | yes |
| office_address_current | native_index | medium | 1 | api | yes |
| office_address_current | none | core | 0 | api | no |
| office_address_current | none | core | 1 | api | no |
| office_address_current | none | medium | 0 | api | no |
| office_address_current | none | medium | 1 | api | no |
| office_address_current | oracle | core | 0 | api | yes |
| office_address_current | oracle | core | 1 | api | yes |
| office_address_current | oracle | medium | 0 | api | yes |
| office_address_current | oracle | medium | 1 | api | yes |
| office_address_current | pull | core | 0 | api | yes |
| office_address_current | pull | core | 1 | api | yes |
| office_address_current | pull | medium | 0 | api | yes |
| office_address_current | pull | medium | 1 | api | yes |
| office_address_current | push_breadcrumb | core | 0 | api | no |
| office_address_current | push_breadcrumb | core | 1 | api | no |
| office_address_current | push_breadcrumb | medium | 0 | api | no |
| office_address_current | push_breadcrumb | medium | 1 | api | no |
| office_address_current | push_breadcrumb_pull | core | 0 | api | yes |
| office_address_current | push_breadcrumb_pull | core | 1 | api | yes |
| office_address_current | push_breadcrumb_pull | medium | 0 | api | yes |
| office_address_current | push_breadcrumb_pull | medium | 1 | api | yes |
| office_address_current | push_pages_upper_bound | core | 0 | api | yes |
| office_address_current | push_pages_upper_bound | core | 1 | api | yes |
| office_address_current | push_pages_upper_bound | medium | 0 | api | yes |
| office_address_current | push_pages_upper_bound | medium | 1 | api | yes |
| onboarding_length | native_grep | core | 0 | api | yes |
| onboarding_length | native_grep | core | 1 | api | yes |
| onboarding_length | native_grep | medium | 0 | api | yes |
| onboarding_length | native_grep | medium | 1 | api | yes |
| onboarding_length | native_index | core | 0 | api | yes |
| onboarding_length | native_index | core | 1 | api | yes |
| onboarding_length | native_index | medium | 0 | api | yes |
| onboarding_length | native_index | medium | 1 | api | yes |
| onboarding_length | none | core | 0 | api | no |
| onboarding_length | none | core | 1 | api | no |
| onboarding_length | none | medium | 0 | api | no |
| onboarding_length | none | medium | 1 | api | no |
| onboarding_length | oracle | core | 0 | api | yes |
| onboarding_length | oracle | core | 1 | api | yes |
| onboarding_length | oracle | medium | 0 | api | yes |
| onboarding_length | oracle | medium | 1 | api | yes |
| onboarding_length | pull | core | 0 | api | yes |
| onboarding_length | pull | core | 1 | api | yes |
| onboarding_length | pull | medium | 0 | api | yes |
| onboarding_length | pull | medium | 1 | api | yes |
| onboarding_length | push_breadcrumb | core | 0 | api | no |
| onboarding_length | push_breadcrumb | core | 1 | api | no |
| onboarding_length | push_breadcrumb | medium | 0 | api | no |
| onboarding_length | push_breadcrumb | medium | 1 | api | no |
| onboarding_length | push_breadcrumb_pull | core | 0 | api | yes |
| onboarding_length | push_breadcrumb_pull | core | 1 | api | yes |
| onboarding_length | push_breadcrumb_pull | medium | 0 | api | yes |
| onboarding_length | push_breadcrumb_pull | medium | 1 | api | yes |
| onboarding_length | push_pages_upper_bound | core | 0 | api | yes |
| onboarding_length | push_pages_upper_bound | core | 1 | api | yes |
| onboarding_length | push_pages_upper_bound | medium | 0 | api | yes |
| onboarding_length | push_pages_upper_bound | medium | 1 | api | yes |
| overtime_request_approver | native_grep | core | 0 | api | yes |
| overtime_request_approver | native_grep | core | 1 | api | yes |
| overtime_request_approver | native_grep | medium | 0 | api | yes |
| overtime_request_approver | native_grep | medium | 1 | api | yes |
| overtime_request_approver | native_index | core | 0 | api | yes |
| overtime_request_approver | native_index | core | 1 | api | yes |
| overtime_request_approver | native_index | medium | 0 | api | yes |
| overtime_request_approver | native_index | medium | 1 | api | yes |
| overtime_request_approver | none | core | 0 | api | no |
| overtime_request_approver | none | core | 1 | api | no |
| overtime_request_approver | none | medium | 0 | api | no |
| overtime_request_approver | none | medium | 1 | api | no |
| overtime_request_approver | oracle | core | 0 | api | no |
| overtime_request_approver | oracle | core | 1 | api | no |
| overtime_request_approver | oracle | medium | 0 | api | no |
| overtime_request_approver | oracle | medium | 1 | api | no |
| overtime_request_approver | pull | core | 0 | api | yes |
| overtime_request_approver | pull | core | 1 | api | yes |
| overtime_request_approver | pull | medium | 0 | api | yes |
| overtime_request_approver | pull | medium | 1 | api | yes |
| overtime_request_approver | push_breadcrumb | core | 0 | api | no |
| overtime_request_approver | push_breadcrumb | core | 1 | api | no |
| overtime_request_approver | push_breadcrumb | medium | 0 | api | no |
| overtime_request_approver | push_breadcrumb | medium | 1 | api | no |
| overtime_request_approver | push_breadcrumb_pull | core | 0 | api | yes |
| overtime_request_approver | push_breadcrumb_pull | core | 1 | api | yes |
| overtime_request_approver | push_breadcrumb_pull | medium | 0 | api | yes |
| overtime_request_approver | push_breadcrumb_pull | medium | 1 | api | yes |
| overtime_request_approver | push_pages_upper_bound | core | 0 | api | no |
| overtime_request_approver | push_pages_upper_bound | core | 1 | api | no |
| overtime_request_approver | push_pages_upper_bound | medium | 0 | api | no |
| overtime_request_approver | push_pages_upper_bound | medium | 1 | api | no |
| person_not_repo | native_grep | core | 0 | api | no |
| person_not_repo | native_grep | core | 1 | api | yes |
| person_not_repo | native_grep | medium | 0 | api | yes |
| person_not_repo | native_grep | medium | 1 | api | yes |
| person_not_repo | native_index | core | 0 | api | no |
| person_not_repo | native_index | core | 1 | api | no |
| person_not_repo | native_index | medium | 0 | api | no |
| person_not_repo | native_index | medium | 1 | api | no |
| person_not_repo | none | core | 0 | api | no |
| person_not_repo | none | core | 1 | api | no |
| person_not_repo | none | medium | 0 | api | no |
| person_not_repo | none | medium | 1 | api | no |
| person_not_repo | oracle | core | 0 | api | yes |
| person_not_repo | oracle | core | 1 | api | yes |
| person_not_repo | oracle | medium | 0 | api | yes |
| person_not_repo | oracle | medium | 1 | api | yes |
| person_not_repo | pull | core | 0 | api | no |
| person_not_repo | pull | core | 1 | api | yes |
| person_not_repo | pull | medium | 0 | api | no |
| person_not_repo | pull | medium | 1 | api | yes |
| person_not_repo | push_breadcrumb | core | 0 | api | no |
| person_not_repo | push_breadcrumb | core | 1 | api | no |
| person_not_repo | push_breadcrumb | medium | 0 | api | no |
| person_not_repo | push_breadcrumb | medium | 1 | api | no |
| person_not_repo | push_breadcrumb_pull | core | 0 | api | no |
| person_not_repo | push_breadcrumb_pull | core | 1 | api | no |
| person_not_repo | push_breadcrumb_pull | medium | 0 | api | no |
| person_not_repo | push_breadcrumb_pull | medium | 1 | api | no |
| person_not_repo | push_pages_upper_bound | core | 0 | api | no |
| person_not_repo | push_pages_upper_bound | core | 1 | api | no |
| person_not_repo | push_pages_upper_bound | medium | 0 | api | no |
| person_not_repo | push_pages_upper_bound | medium | 1 | api | no |
| pinemarsh_goal_rationale | native_grep | core | 0 | api | yes |
| pinemarsh_goal_rationale | native_grep | core | 1 | api | yes |
| pinemarsh_goal_rationale | native_grep | medium | 0 | api | yes |
| pinemarsh_goal_rationale | native_grep | medium | 1 | api | no |
| pinemarsh_goal_rationale | native_index | core | 0 | api | yes |
| pinemarsh_goal_rationale | native_index | core | 1 | api | yes |
| pinemarsh_goal_rationale | native_index | medium | 0 | api | yes |
| pinemarsh_goal_rationale | native_index | medium | 1 | api | yes |
| pinemarsh_goal_rationale | none | core | 0 | api | no |
| pinemarsh_goal_rationale | none | core | 1 | api | no |
| pinemarsh_goal_rationale | none | medium | 0 | api | no |
| pinemarsh_goal_rationale | none | medium | 1 | api | no |
| pinemarsh_goal_rationale | oracle | core | 0 | api | yes |
| pinemarsh_goal_rationale | oracle | core | 1 | api | yes |
| pinemarsh_goal_rationale | oracle | medium | 0 | api | yes |
| pinemarsh_goal_rationale | oracle | medium | 1 | api | yes |
| pinemarsh_goal_rationale | pull | core | 0 | api | yes |
| pinemarsh_goal_rationale | pull | core | 1 | api | yes |
| pinemarsh_goal_rationale | pull | medium | 0 | api | yes |
| pinemarsh_goal_rationale | pull | medium | 1 | api | no |
| pinemarsh_goal_rationale | push_breadcrumb | core | 0 | api | no |
| pinemarsh_goal_rationale | push_breadcrumb | core | 1 | api | no |
| pinemarsh_goal_rationale | push_breadcrumb | medium | 0 | api | no |
| pinemarsh_goal_rationale | push_breadcrumb | medium | 1 | api | no |
| pinemarsh_goal_rationale | push_breadcrumb_pull | core | 0 | api | yes |
| pinemarsh_goal_rationale | push_breadcrumb_pull | core | 1 | api | yes |
| pinemarsh_goal_rationale | push_breadcrumb_pull | medium | 0 | api | yes |
| pinemarsh_goal_rationale | push_breadcrumb_pull | medium | 1 | api | yes |
| pinemarsh_goal_rationale | push_pages_upper_bound | core | 0 | api | no |
| pinemarsh_goal_rationale | push_pages_upper_bound | core | 1 | api | no |
| pinemarsh_goal_rationale | push_pages_upper_bound | medium | 0 | api | no |
| pinemarsh_goal_rationale | push_pages_upper_bound | medium | 1 | api | no |
| portal_design_reviewer | native_grep | core | 0 | api | yes |
| portal_design_reviewer | native_grep | core | 1 | api | yes |
| portal_design_reviewer | native_grep | medium | 0 | api | no |
| portal_design_reviewer | native_grep | medium | 1 | api | no |
| portal_design_reviewer | native_index | core | 0 | api | yes |
| portal_design_reviewer | native_index | core | 1 | api | yes |
| portal_design_reviewer | native_index | medium | 0 | api | yes |
| portal_design_reviewer | native_index | medium | 1 | api | yes |
| portal_design_reviewer | none | core | 0 | api | no |
| portal_design_reviewer | none | core | 1 | api | no |
| portal_design_reviewer | none | medium | 0 | api | no |
| portal_design_reviewer | none | medium | 1 | api | no |
| portal_design_reviewer | oracle | core | 0 | api | yes |
| portal_design_reviewer | oracle | core | 1 | api | yes |
| portal_design_reviewer | oracle | medium | 0 | api | yes |
| portal_design_reviewer | oracle | medium | 1 | api | yes |
| portal_design_reviewer | pull | core | 0 | api | yes |
| portal_design_reviewer | pull | core | 1 | api | yes |
| portal_design_reviewer | pull | medium | 0 | api | yes |
| portal_design_reviewer | pull | medium | 1 | api | no |
| portal_design_reviewer | push_breadcrumb | core | 0 | api | no |
| portal_design_reviewer | push_breadcrumb | core | 1 | api | no |
| portal_design_reviewer | push_breadcrumb | medium | 0 | api | no |
| portal_design_reviewer | push_breadcrumb | medium | 1 | api | no |
| portal_design_reviewer | push_breadcrumb_pull | core | 0 | api | yes |
| portal_design_reviewer | push_breadcrumb_pull | core | 1 | api | yes |
| portal_design_reviewer | push_breadcrumb_pull | medium | 0 | api | yes |
| portal_design_reviewer | push_breadcrumb_pull | medium | 1 | api | yes |
| portal_design_reviewer | push_pages_upper_bound | core | 0 | api | no |
| portal_design_reviewer | push_pages_upper_bound | core | 1 | api | no |
| portal_design_reviewer | push_pages_upper_bound | medium | 0 | api | no |
| portal_design_reviewer | push_pages_upper_bound | medium | 1 | api | no |
| pto_allowance | native_grep | core | 0 | api | yes |
| pto_allowance | native_grep | core | 1 | api | yes |
| pto_allowance | native_grep | medium | 0 | api | no |
| pto_allowance | native_grep | medium | 1 | api | yes |
| pto_allowance | native_index | core | 0 | api | yes |
| pto_allowance | native_index | core | 1 | api | no |
| pto_allowance | native_index | medium | 0 | api | no |
| pto_allowance | native_index | medium | 1 | api | yes |
| pto_allowance | none | core | 0 | api | no |
| pto_allowance | none | core | 1 | api | no |
| pto_allowance | none | medium | 0 | api | no |
| pto_allowance | none | medium | 1 | api | no |
| pto_allowance | oracle | core | 0 | api | yes |
| pto_allowance | oracle | core | 1 | api | yes |
| pto_allowance | oracle | medium | 0 | api | yes |
| pto_allowance | oracle | medium | 1 | api | yes |
| pto_allowance | pull | core | 0 | api | yes |
| pto_allowance | pull | core | 1 | api | yes |
| pto_allowance | pull | medium | 0 | api | yes |
| pto_allowance | pull | medium | 1 | api | yes |
| pto_allowance | push_breadcrumb | core | 0 | api | no |
| pto_allowance | push_breadcrumb | core | 1 | api | no |
| pto_allowance | push_breadcrumb | medium | 0 | api | no |
| pto_allowance | push_breadcrumb | medium | 1 | api | no |
| pto_allowance | push_breadcrumb_pull | core | 0 | api | yes |
| pto_allowance | push_breadcrumb_pull | core | 1 | api | yes |
| pto_allowance | push_breadcrumb_pull | medium | 0 | api | yes |
| pto_allowance | push_breadcrumb_pull | medium | 1 | api | yes |
| pto_allowance | push_pages_upper_bound | core | 0 | api | yes |
| pto_allowance | push_pages_upper_bound | core | 1 | api | yes |
| pto_allowance | push_pages_upper_bound | medium | 0 | api | yes |
| pto_allowance | push_pages_upper_bound | medium | 1 | api | yes |
| ratecard_tooling_owner | native_grep | core | 0 | api | yes |
| ratecard_tooling_owner | native_grep | core | 1 | api | no |
| ratecard_tooling_owner | native_grep | medium | 0 | api | no |
| ratecard_tooling_owner | native_grep | medium | 1 | api | no |
| ratecard_tooling_owner | native_index | core | 0 | api | no |
| ratecard_tooling_owner | native_index | core | 1 | api | no |
| ratecard_tooling_owner | native_index | medium | 0 | api | no |
| ratecard_tooling_owner | native_index | medium | 1 | api | no |
| ratecard_tooling_owner | none | core | 0 | api | no |
| ratecard_tooling_owner | none | core | 1 | api | no |
| ratecard_tooling_owner | none | medium | 0 | api | no |
| ratecard_tooling_owner | none | medium | 1 | api | no |
| ratecard_tooling_owner | oracle | core | 0 | api | yes |
| ratecard_tooling_owner | oracle | core | 1 | api | yes |
| ratecard_tooling_owner | oracle | medium | 0 | api | yes |
| ratecard_tooling_owner | oracle | medium | 1 | api | yes |
| ratecard_tooling_owner | pull | core | 0 | api | no |
| ratecard_tooling_owner | pull | core | 1 | api | no |
| ratecard_tooling_owner | pull | medium | 0 | api | no |
| ratecard_tooling_owner | pull | medium | 1 | api | no |
| ratecard_tooling_owner | push_breadcrumb | core | 0 | api | no |
| ratecard_tooling_owner | push_breadcrumb | core | 1 | api | no |
| ratecard_tooling_owner | push_breadcrumb | medium | 0 | api | no |
| ratecard_tooling_owner | push_breadcrumb | medium | 1 | api | no |
| ratecard_tooling_owner | push_breadcrumb_pull | core | 0 | api | no |
| ratecard_tooling_owner | push_breadcrumb_pull | core | 1 | api | no |
| ratecard_tooling_owner | push_breadcrumb_pull | medium | 0 | api | no |
| ratecard_tooling_owner | push_breadcrumb_pull | medium | 1 | api | no |
| ratecard_tooling_owner | push_pages_upper_bound | core | 0 | api | no |
| ratecard_tooling_owner | push_pages_upper_bound | core | 1 | api | no |
| ratecard_tooling_owner | push_pages_upper_bound | medium | 0 | api | no |
| ratecard_tooling_owner | push_pages_upper_bound | medium | 1 | api | no |
| regional_office_onboarding_lesson | native_grep | core | 0 | api | yes |
| regional_office_onboarding_lesson | native_grep | core | 1 | api | yes |
| regional_office_onboarding_lesson | native_index | core | 0 | api | yes |
| regional_office_onboarding_lesson | native_index | core | 1 | api | yes |
| regional_office_onboarding_lesson | none | core | 0 | api | no |
| regional_office_onboarding_lesson | none | core | 1 | api | no |
| regional_office_onboarding_lesson | oracle | core | 0 | api | yes |
| regional_office_onboarding_lesson | oracle | core | 1 | api | yes |
| regional_office_onboarding_lesson | pull | core | 0 | api | yes |
| regional_office_onboarding_lesson | pull | core | 1 | api | yes |
| regional_office_onboarding_lesson | push_breadcrumb | core | 0 | api | no |
| regional_office_onboarding_lesson | push_breadcrumb | core | 1 | api | no |
| regional_office_onboarding_lesson | push_breadcrumb_pull | core | 0 | api | yes |
| regional_office_onboarding_lesson | push_breadcrumb_pull | core | 1 | api | yes |
| regional_office_onboarding_lesson | push_pages_upper_bound | core | 0 | api | yes |
| regional_office_onboarding_lesson | push_pages_upper_bound | core | 1 | api | yes |
| remote_equipment_stipend_cap | native_grep | core | 0 | api | yes |
| remote_equipment_stipend_cap | native_grep | core | 1 | api | yes |
| remote_equipment_stipend_cap | native_grep | medium | 0 | api | yes |
| remote_equipment_stipend_cap | native_grep | medium | 1 | api | yes |
| remote_equipment_stipend_cap | native_index | core | 0 | api | yes |
| remote_equipment_stipend_cap | native_index | core | 1 | api | yes |
| remote_equipment_stipend_cap | native_index | medium | 0 | api | yes |
| remote_equipment_stipend_cap | native_index | medium | 1 | api | yes |
| remote_equipment_stipend_cap | none | core | 0 | api | no |
| remote_equipment_stipend_cap | none | core | 1 | api | no |
| remote_equipment_stipend_cap | none | medium | 0 | api | no |
| remote_equipment_stipend_cap | none | medium | 1 | api | no |
| remote_equipment_stipend_cap | oracle | core | 0 | api | yes |
| remote_equipment_stipend_cap | oracle | core | 1 | api | yes |
| remote_equipment_stipend_cap | oracle | medium | 0 | api | yes |
| remote_equipment_stipend_cap | oracle | medium | 1 | api | yes |
| remote_equipment_stipend_cap | pull | core | 0 | api | yes |
| remote_equipment_stipend_cap | pull | core | 1 | api | yes |
| remote_equipment_stipend_cap | pull | medium | 0 | api | yes |
| remote_equipment_stipend_cap | pull | medium | 1 | api | yes |
| remote_equipment_stipend_cap | push_breadcrumb | core | 0 | api | no |
| remote_equipment_stipend_cap | push_breadcrumb | core | 1 | api | no |
| remote_equipment_stipend_cap | push_breadcrumb | medium | 0 | api | no |
| remote_equipment_stipend_cap | push_breadcrumb | medium | 1 | api | no |
| remote_equipment_stipend_cap | push_breadcrumb_pull | core | 0 | api | yes |
| remote_equipment_stipend_cap | push_breadcrumb_pull | core | 1 | api | yes |
| remote_equipment_stipend_cap | push_breadcrumb_pull | medium | 0 | api | yes |
| remote_equipment_stipend_cap | push_breadcrumb_pull | medium | 1 | api | yes |
| remote_equipment_stipend_cap | push_pages_upper_bound | core | 0 | api | no |
| remote_equipment_stipend_cap | push_pages_upper_bound | core | 1 | api | no |
| remote_equipment_stipend_cap | push_pages_upper_bound | medium | 0 | api | no |
| remote_equipment_stipend_cap | push_pages_upper_bound | medium | 1 | api | no |
| repo_not_person | native_grep | core | 0 | api | yes |
| repo_not_person | native_grep | core | 1 | api | yes |
| repo_not_person | native_grep | medium | 0 | api | yes |
| repo_not_person | native_grep | medium | 1 | api | yes |
| repo_not_person | native_index | core | 0 | api | yes |
| repo_not_person | native_index | core | 1 | api | yes |
| repo_not_person | native_index | medium | 0 | api | yes |
| repo_not_person | native_index | medium | 1 | api | yes |
| repo_not_person | none | core | 0 | api | no |
| repo_not_person | none | core | 1 | api | no |
| repo_not_person | none | medium | 0 | api | no |
| repo_not_person | none | medium | 1 | api | no |
| repo_not_person | oracle | core | 0 | api | yes |
| repo_not_person | oracle | core | 1 | api | yes |
| repo_not_person | oracle | medium | 0 | api | yes |
| repo_not_person | oracle | medium | 1 | api | yes |
| repo_not_person | pull | core | 0 | api | no |
| repo_not_person | pull | core | 1 | api | no |
| repo_not_person | pull | medium | 0 | api | yes |
| repo_not_person | pull | medium | 1 | api | no |
| repo_not_person | push_breadcrumb | core | 0 | api | no |
| repo_not_person | push_breadcrumb | core | 1 | api | no |
| repo_not_person | push_breadcrumb | medium | 0 | api | no |
| repo_not_person | push_breadcrumb | medium | 1 | api | no |
| repo_not_person | push_breadcrumb_pull | core | 0 | api | no |
| repo_not_person | push_breadcrumb_pull | core | 1 | api | no |
| repo_not_person | push_breadcrumb_pull | medium | 0 | api | no |
| repo_not_person | push_breadcrumb_pull | medium | 1 | api | no |
| repo_not_person | push_pages_upper_bound | core | 0 | api | no |
| repo_not_person | push_pages_upper_bound | core | 1 | api | no |
| repo_not_person | push_pages_upper_bound | medium | 0 | api | no |
| repo_not_person | push_pages_upper_bound | medium | 1 | api | no |
| spend_approver_named | native_grep | core | 0 | api | yes |
| spend_approver_named | native_grep | core | 1 | api | yes |
| spend_approver_named | native_grep | medium | 0 | api | yes |
| spend_approver_named | native_grep | medium | 1 | api | yes |
| spend_approver_named | native_index | core | 0 | api | yes |
| spend_approver_named | native_index | core | 1 | api | yes |
| spend_approver_named | native_index | medium | 0 | api | yes |
| spend_approver_named | native_index | medium | 1 | api | yes |
| spend_approver_named | none | core | 0 | api | no |
| spend_approver_named | none | core | 1 | api | no |
| spend_approver_named | none | medium | 0 | api | no |
| spend_approver_named | none | medium | 1 | api | no |
| spend_approver_named | oracle | core | 0 | api | yes |
| spend_approver_named | oracle | core | 1 | api | yes |
| spend_approver_named | oracle | medium | 0 | api | yes |
| spend_approver_named | oracle | medium | 1 | api | yes |
| spend_approver_named | pull | core | 0 | api | yes |
| spend_approver_named | pull | core | 1 | api | yes |
| spend_approver_named | pull | medium | 0 | api | yes |
| spend_approver_named | pull | medium | 1 | api | yes |
| spend_approver_named | push_breadcrumb | core | 0 | api | no |
| spend_approver_named | push_breadcrumb | core | 1 | api | no |
| spend_approver_named | push_breadcrumb | medium | 0 | api | no |
| spend_approver_named | push_breadcrumb | medium | 1 | api | no |
| spend_approver_named | push_breadcrumb_pull | core | 0 | api | yes |
| spend_approver_named | push_breadcrumb_pull | core | 1 | api | yes |
| spend_approver_named | push_breadcrumb_pull | medium | 0 | api | yes |
| spend_approver_named | push_breadcrumb_pull | medium | 1 | api | yes |
| spend_approver_named | push_pages_upper_bound | core | 0 | api | no |
| spend_approver_named | push_pages_upper_bound | core | 1 | api | no |
| spend_approver_named | push_pages_upper_bound | medium | 0 | api | no |
| spend_approver_named | push_pages_upper_bound | medium | 1 | api | no |
| standup_time_current | native_grep | core | 0 | api | yes |
| standup_time_current | native_grep | core | 1 | api | yes |
| standup_time_current | native_grep | medium | 0 | api | no |
| standup_time_current | native_grep | medium | 1 | api | no |
| standup_time_current | native_index | core | 0 | api | no |
| standup_time_current | native_index | core | 1 | api | no |
| standup_time_current | native_index | medium | 0 | api | no |
| standup_time_current | native_index | medium | 1 | api | no |
| standup_time_current | none | core | 0 | api | no |
| standup_time_current | none | core | 1 | api | no |
| standup_time_current | none | medium | 0 | api | no |
| standup_time_current | none | medium | 1 | api | no |
| standup_time_current | oracle | core | 0 | api | yes |
| standup_time_current | oracle | core | 1 | api | yes |
| standup_time_current | oracle | medium | 0 | api | yes |
| standup_time_current | oracle | medium | 1 | api | yes |
| standup_time_current | pull | core | 0 | api | yes |
| standup_time_current | pull | core | 1 | api | yes |
| standup_time_current | pull | medium | 0 | api | yes |
| standup_time_current | pull | medium | 1 | api | yes |
| standup_time_current | push_breadcrumb | core | 0 | api | no |
| standup_time_current | push_breadcrumb | core | 1 | api | no |
| standup_time_current | push_breadcrumb | medium | 0 | api | no |
| standup_time_current | push_breadcrumb | medium | 1 | api | no |
| standup_time_current | push_breadcrumb_pull | core | 0 | api | yes |
| standup_time_current | push_breadcrumb_pull | core | 1 | api | yes |
| standup_time_current | push_breadcrumb_pull | medium | 0 | api | yes |
| standup_time_current | push_breadcrumb_pull | medium | 1 | api | yes |
| standup_time_current | push_pages_upper_bound | core | 0 | api | yes |
| standup_time_current | push_pages_upper_bound | core | 1 | api | yes |
| standup_time_current | push_pages_upper_bound | medium | 0 | api | yes |
| standup_time_current | push_pages_upper_bound | medium | 1 | api | yes |
| subsidiary_onboarding_lesson | native_grep | core | 0 | api | yes |
| subsidiary_onboarding_lesson | native_grep | core | 1 | api | yes |
| subsidiary_onboarding_lesson | native_grep | medium | 0 | api | yes |
| subsidiary_onboarding_lesson | native_grep | medium | 1 | api | yes |
| subsidiary_onboarding_lesson | native_index | core | 0 | api | yes |
| subsidiary_onboarding_lesson | native_index | core | 1 | api | yes |
| subsidiary_onboarding_lesson | native_index | medium | 0 | api | yes |
| subsidiary_onboarding_lesson | native_index | medium | 1 | api | yes |
| subsidiary_onboarding_lesson | none | core | 0 | api | no |
| subsidiary_onboarding_lesson | none | core | 1 | api | no |
| subsidiary_onboarding_lesson | none | medium | 0 | api | no |
| subsidiary_onboarding_lesson | none | medium | 1 | api | no |
| subsidiary_onboarding_lesson | oracle | core | 0 | api | yes |
| subsidiary_onboarding_lesson | oracle | core | 1 | api | yes |
| subsidiary_onboarding_lesson | oracle | medium | 0 | api | yes |
| subsidiary_onboarding_lesson | oracle | medium | 1 | api | yes |
| subsidiary_onboarding_lesson | pull | core | 0 | api | yes |
| subsidiary_onboarding_lesson | pull | core | 1 | api | yes |
| subsidiary_onboarding_lesson | pull | medium | 0 | api | yes |
| subsidiary_onboarding_lesson | pull | medium | 1 | api | yes |
| subsidiary_onboarding_lesson | push_breadcrumb | core | 0 | api | no |
| subsidiary_onboarding_lesson | push_breadcrumb | core | 1 | api | no |
| subsidiary_onboarding_lesson | push_breadcrumb | medium | 0 | api | no |
| subsidiary_onboarding_lesson | push_breadcrumb | medium | 1 | api | no |
| subsidiary_onboarding_lesson | push_breadcrumb_pull | core | 0 | api | yes |
| subsidiary_onboarding_lesson | push_breadcrumb_pull | core | 1 | api | yes |
| subsidiary_onboarding_lesson | push_breadcrumb_pull | medium | 0 | api | yes |
| subsidiary_onboarding_lesson | push_breadcrumb_pull | medium | 1 | api | yes |
| subsidiary_onboarding_lesson | push_pages_upper_bound | core | 0 | api | no |
| subsidiary_onboarding_lesson | push_pages_upper_bound | core | 1 | api | no |
| subsidiary_onboarding_lesson | push_pages_upper_bound | medium | 0 | api | no |
| subsidiary_onboarding_lesson | push_pages_upper_bound | medium | 1 | api | no |
| surname_is_ambiguous | native_grep | core | 0 | api | yes |
| surname_is_ambiguous | native_grep | core | 1 | api | no |
| surname_is_ambiguous | native_grep | medium | 0 | api | no |
| surname_is_ambiguous | native_grep | medium | 1 | api | no |
| surname_is_ambiguous | native_index | core | 0 | api | yes |
| surname_is_ambiguous | native_index | core | 1 | api | yes |
| surname_is_ambiguous | native_index | medium | 0 | api | no |
| surname_is_ambiguous | native_index | medium | 1 | api | no |
| surname_is_ambiguous | none | core | 0 | api | no |
| surname_is_ambiguous | none | core | 1 | api | no |
| surname_is_ambiguous | none | medium | 0 | api | no |
| surname_is_ambiguous | none | medium | 1 | api | no |
| surname_is_ambiguous | oracle | core | 0 | api | yes |
| surname_is_ambiguous | oracle | core | 1 | api | yes |
| surname_is_ambiguous | oracle | medium | 0 | api | yes |
| surname_is_ambiguous | oracle | medium | 1 | api | yes |
| surname_is_ambiguous | pull | core | 0 | api | no |
| surname_is_ambiguous | pull | core | 1 | api | no |
| surname_is_ambiguous | pull | medium | 0 | api | no |
| surname_is_ambiguous | pull | medium | 1 | api | no |
| surname_is_ambiguous | push_breadcrumb | core | 0 | api | no |
| surname_is_ambiguous | push_breadcrumb | core | 1 | api | no |
| surname_is_ambiguous | push_breadcrumb | medium | 0 | api | no |
| surname_is_ambiguous | push_breadcrumb | medium | 1 | api | no |
| surname_is_ambiguous | push_breadcrumb_pull | core | 0 | api | no |
| surname_is_ambiguous | push_breadcrumb_pull | core | 1 | api | no |
| surname_is_ambiguous | push_breadcrumb_pull | medium | 0 | api | no |
| surname_is_ambiguous | push_breadcrumb_pull | medium | 1 | api | no |
| surname_is_ambiguous | push_pages_upper_bound | core | 0 | api | no |
| surname_is_ambiguous | push_pages_upper_bound | core | 1 | api | no |
| surname_is_ambiguous | push_pages_upper_bound | medium | 0 | api | no |
| surname_is_ambiguous | push_pages_upper_bound | medium | 1 | api | yes |
| tamsin_ferro_role_change | native_grep | core | 0 | api | yes |
| tamsin_ferro_role_change | native_grep | core | 1 | api | yes |
| tamsin_ferro_role_change | native_grep | medium | 0 | api | yes |
| tamsin_ferro_role_change | native_grep | medium | 1 | api | yes |
| tamsin_ferro_role_change | native_index | core | 0 | api | yes |
| tamsin_ferro_role_change | native_index | core | 1 | api | yes |
| tamsin_ferro_role_change | native_index | medium | 0 | api | yes |
| tamsin_ferro_role_change | native_index | medium | 1 | api | yes |
| tamsin_ferro_role_change | none | core | 0 | api | no |
| tamsin_ferro_role_change | none | core | 1 | api | no |
| tamsin_ferro_role_change | none | medium | 0 | api | no |
| tamsin_ferro_role_change | none | medium | 1 | api | no |
| tamsin_ferro_role_change | oracle | core | 0 | api | yes |
| tamsin_ferro_role_change | oracle | core | 1 | api | yes |
| tamsin_ferro_role_change | oracle | medium | 0 | api | yes |
| tamsin_ferro_role_change | oracle | medium | 1 | api | yes |
| tamsin_ferro_role_change | pull | core | 0 | api | yes |
| tamsin_ferro_role_change | pull | core | 1 | api | yes |
| tamsin_ferro_role_change | pull | medium | 0 | api | yes |
| tamsin_ferro_role_change | pull | medium | 1 | api | yes |
| tamsin_ferro_role_change | push_breadcrumb | core | 0 | api | no |
| tamsin_ferro_role_change | push_breadcrumb | core | 1 | api | no |
| tamsin_ferro_role_change | push_breadcrumb | medium | 0 | api | no |
| tamsin_ferro_role_change | push_breadcrumb | medium | 1 | api | no |
| tamsin_ferro_role_change | push_breadcrumb_pull | core | 0 | api | yes |
| tamsin_ferro_role_change | push_breadcrumb_pull | core | 1 | api | yes |
| tamsin_ferro_role_change | push_breadcrumb_pull | medium | 0 | api | yes |
| tamsin_ferro_role_change | push_breadcrumb_pull | medium | 1 | api | yes |
| tamsin_ferro_role_change | push_pages_upper_bound | core | 0 | api | yes |
| tamsin_ferro_role_change | push_pages_upper_bound | core | 1 | api | yes |
| tamsin_ferro_role_change | push_pages_upper_bound | medium | 0 | api | yes |
| tamsin_ferro_role_change | push_pages_upper_bound | medium | 1 | api | yes |
| thorncastle_first_contact | native_grep | core | 0 | api | yes |
| thorncastle_first_contact | native_grep | core | 1 | api | yes |
| thorncastle_first_contact | native_grep | medium | 0 | api | yes |
| thorncastle_first_contact | native_grep | medium | 1 | api | yes |
| thorncastle_first_contact | native_index | core | 0 | api | yes |
| thorncastle_first_contact | native_index | core | 1 | api | yes |
| thorncastle_first_contact | native_index | medium | 0 | api | yes |
| thorncastle_first_contact | native_index | medium | 1 | api | yes |
| thorncastle_first_contact | none | core | 0 | api | no |
| thorncastle_first_contact | none | core | 1 | api | no |
| thorncastle_first_contact | none | medium | 0 | api | no |
| thorncastle_first_contact | none | medium | 1 | api | no |
| thorncastle_first_contact | oracle | core | 0 | api | yes |
| thorncastle_first_contact | oracle | core | 1 | api | yes |
| thorncastle_first_contact | oracle | medium | 0 | api | yes |
| thorncastle_first_contact | oracle | medium | 1 | api | yes |
| thorncastle_first_contact | pull | core | 0 | api | yes |
| thorncastle_first_contact | pull | core | 1 | api | yes |
| thorncastle_first_contact | pull | medium | 0 | api | yes |
| thorncastle_first_contact | pull | medium | 1 | api | yes |
| thorncastle_first_contact | push_breadcrumb | core | 0 | api | no |
| thorncastle_first_contact | push_breadcrumb | core | 1 | api | no |
| thorncastle_first_contact | push_breadcrumb | medium | 0 | api | no |
| thorncastle_first_contact | push_breadcrumb | medium | 1 | api | no |
| thorncastle_first_contact | push_breadcrumb_pull | core | 0 | api | yes |
| thorncastle_first_contact | push_breadcrumb_pull | core | 1 | api | yes |
| thorncastle_first_contact | push_breadcrumb_pull | medium | 0 | api | yes |
| thorncastle_first_contact | push_breadcrumb_pull | medium | 1 | api | yes |
| thorncastle_first_contact | push_pages_upper_bound | core | 0 | api | yes |
| thorncastle_first_contact | push_pages_upper_bound | core | 1 | api | yes |
| thorncastle_first_contact | push_pages_upper_bound | medium | 0 | api | yes |
| thorncastle_first_contact | push_pages_upper_bound | medium | 1 | api | yes |
| triform_vendor_consolidation | native_grep | core | 0 | api | yes |
| triform_vendor_consolidation | native_grep | core | 1 | api | yes |
| triform_vendor_consolidation | native_grep | medium | 0 | api | yes |
| triform_vendor_consolidation | native_grep | medium | 1 | api | yes |
| triform_vendor_consolidation | native_index | core | 0 | api | yes |
| triform_vendor_consolidation | native_index | core | 1 | api | yes |
| triform_vendor_consolidation | native_index | medium | 0 | api | yes |
| triform_vendor_consolidation | native_index | medium | 1 | api | yes |
| triform_vendor_consolidation | none | core | 0 | api | no |
| triform_vendor_consolidation | none | core | 1 | api | no |
| triform_vendor_consolidation | none | medium | 0 | api | no |
| triform_vendor_consolidation | none | medium | 1 | api | no |
| triform_vendor_consolidation | oracle | core | 0 | api | yes |
| triform_vendor_consolidation | oracle | core | 1 | api | yes |
| triform_vendor_consolidation | oracle | medium | 0 | api | yes |
| triform_vendor_consolidation | oracle | medium | 1 | api | yes |
| triform_vendor_consolidation | pull | core | 0 | api | yes |
| triform_vendor_consolidation | pull | core | 1 | api | yes |
| triform_vendor_consolidation | pull | medium | 0 | api | yes |
| triform_vendor_consolidation | pull | medium | 1 | api | yes |
| triform_vendor_consolidation | push_breadcrumb | core | 0 | api | no |
| triform_vendor_consolidation | push_breadcrumb | core | 1 | api | no |
| triform_vendor_consolidation | push_breadcrumb | medium | 0 | api | no |
| triform_vendor_consolidation | push_breadcrumb | medium | 1 | api | no |
| triform_vendor_consolidation | push_breadcrumb_pull | core | 0 | api | yes |
| triform_vendor_consolidation | push_breadcrumb_pull | core | 1 | api | yes |
| triform_vendor_consolidation | push_breadcrumb_pull | medium | 0 | api | yes |
| triform_vendor_consolidation | push_breadcrumb_pull | medium | 1 | api | yes |
| triform_vendor_consolidation | push_pages_upper_bound | core | 0 | api | no |
| triform_vendor_consolidation | push_pages_upper_bound | core | 1 | api | no |
| triform_vendor_consolidation | push_pages_upper_bound | medium | 0 | api | no |
| triform_vendor_consolidation | push_pages_upper_bound | medium | 1 | api | no |

## PULL no-call rate (first-class result)

`no_call_rate = count(recall_called=False) / count(PULL rollouts)`, per (probe_class, corpus_scale). The thesis under test predicts a non-trivial rate; a rate near 0.0 across every group means the free data do NOT evidence it.

| probe_class | corpus_scale | n | no_call_rate |
| --- | --- | --- | --- |
| abstention | core | 4 | 0.000 |
| abstention | medium | 4 | 0.000 |
| aggregation | core | 6 | 0.000 |
| aggregation | medium | 5 | 0.000 |
| contradiction | core | 6 | 0.000 |
| contradiction | medium | 6 | 0.000 |
| disambiguation | core | 8 | 0.000 |
| disambiguation | medium | 8 | 0.000 |
| distractor_robustness | core | 4 | 0.000 |
| distractor_robustness | medium | 4 | 0.000 |
| follow_through | core | 12 | 0.000 |
| follow_through | medium | 11 | 0.000 |
| multi_hop | core | 4 | 0.000 |
| multi_hop | medium | 3 | 0.000 |
| negative_knowledge | core | 2 | 0.000 |
| redundancy | core | 8 | 0.000 |
| redundancy | medium | 8 | 0.000 |
| single_hop | core | 16 | 0.000 |
| single_hop | medium | 16 | 0.000 |
| temporal | core | 12 | 0.000 |
| temporal | medium | 12 | 0.000 |

## Cost per correct answer (athenaeum#1734)

`cost_per_correct = (read_input_tokens + read_output_tokens + write_tokens_amortized) / correct_n` -- read cost only in Phase 1. Phase 2 adds write cost amortised over the full probe set at that scale (design doc §6); for a non-native arm `write_tokens_amortized` is the native writer's own amortised share PLUS the librarian's filing share (athenaeum#1870, ruling athenaeum#1830), never the filing share alone -- `write_native_share_amortized` prints that native portion separately so neither component is hidden, and the raw (unamortised) sum is printed alongside. `n/a` in the native-share columns means either no write data at all, or (for a native arm) that the whole combined figure already is native, so there is no separate share to print; `not-captured` means an Athenaeum arm's native share specifically was never captured, so the figure shown is filing-only -- see the design doc. `undefined` (never `inf`/`0`) means zero correct answers in the cell -- there is no denominator.

`cache_weighted_tokens` (athenaeum#2305) folds cache activity into `cost_per_correct`'s numerator at the same multipliers athenaeum#239 already bills real cache activity at (a cache write = 1.25x a plain input token, a cache read = 0.10x) -- counting a PUSH/ORACLE arm's injected context on EVERY later turn it stays resident in the model's context, which a plain input+output-tokens view would otherwise price at zero past the first turn. `n/a` means no row in this group ever recorded cache usage (a pre-athenaeum#2305 row), never a measured zero.

`injected_bytes` (athenaeum#2305) is the summed UTF-8 byte length of the content actually injected for this cell's rows (PUSH/ORACLE arms only) -- reported alongside cost, never folded into `cost_per_correct`'s arithmetic. `n/a` means no row in this group recorded it.

| probe_class | corpus_scale | arm | n | correct_n | read_tokens | cache_weighted_tokens | cost_per_correct | injected_bytes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| abstention | core | native_grep | 3 | 2 | 17120 | 0.0 | 8560.0 | n/a |
| abstention | core | native_index | 6 | 5 | 70958 | 0.0 | 14191.6 | n/a |
| abstention | core | none | 6 | 6 | 1828 | 0.0 | 304.7 | n/a |
| abstention | core | oracle | 6 | 6 | 1656 | 0.0 | 276.0 | 0 |
| abstention | core | pull | 4 | 4 | 111130 | 0.0 | 27782.5 | n/a |
| abstention | core | push_breadcrumb | 6 | 6 | 2958 | 0.0 | 493.0 | 3462 |
| abstention | core | push_breadcrumb_pull | 6 | 5 | 214993 | 0.0 | 42998.6 | 3462 |
| abstention | core | push_pages_upper_bound | 6 | 6 | 6967 | 0.0 | 1161.2 | 17636 |
| abstention | medium | native_grep | 4 | 4 | 20835 | 0.0 | 5208.8 | n/a |
| abstention | medium | native_index | 6 | 5 | 97021 | 0.0 | 19404.2 | n/a |
| abstention | medium | none | 6 | 6 | 1734 | 0.0 | 289.0 | n/a |
| abstention | medium | oracle | 6 | 6 | 1710 | 0.0 | 285.0 | 0 |
| abstention | medium | pull | 4 | 4 | 99468 | 0.0 | 24867.0 | n/a |
| abstention | medium | push_breadcrumb | 6 | 3 | 3204 | 0.0 | 1068.0 | 3644 |
| abstention | medium | push_breadcrumb_pull | 5 | 5 | 117766 | 0.0 | 23553.2 | 2960 |
| abstention | medium | push_pages_upper_bound | 6 | 6 | 7110 | 0.0 | 1185.0 | 17134 |
| aggregation | core | native_grep | 5 | 5 | 81051 | 0.0 | 16210.2 | n/a |
| aggregation | core | native_index | 6 | 0 | 85127 | 0.0 | undefined | n/a |
| aggregation | core | none | 6 | 0 | 1771 | 0.0 | undefined | n/a |
| aggregation | core | oracle | 6 | 4 | 9350 | 0.0 | 2337.5 | 18504 |
| aggregation | core | pull | 6 | 1 | 92475 | 0.0 | 92475.0 | n/a |
| aggregation | core | push_breadcrumb | 6 | 0 | 3346 | 0.0 | undefined | 3492 |
| aggregation | core | push_breadcrumb_pull | 6 | 2 | 66684 | 0.0 | 33342.0 | 3492 |
| aggregation | core | push_pages_upper_bound | 6 | 0 | 6981 | 0.0 | undefined | 15076 |
| aggregation | medium | native_grep | 2 | 2 | 22635 | 0.0 | 11317.5 | n/a |
| aggregation | medium | native_index | 6 | 2 | 184398 | 0.0 | 92199.0 | n/a |
| aggregation | medium | none | 6 | 0 | 1840 | 0.0 | undefined | n/a |
| aggregation | medium | oracle | 6 | 3 | 9282 | 0.0 | 3094.0 | 18504 |
| aggregation | medium | pull | 5 | 0 | 142217 | 0.0 | undefined | n/a |
| aggregation | medium | push_breadcrumb | 6 | 0 | 3403 | 0.0 | undefined | 3758 |
| aggregation | medium | push_breadcrumb_pull | 6 | 1 | 110699 | 0.0 | 110699.0 | 3758 |
| aggregation | medium | push_pages_upper_bound | 6 | 0 | 7332 | 0.0 | undefined | 16582 |
| contradiction | core | native_grep | 6 | 4 | 31537 | 0.0 | 7884.2 | n/a |
| contradiction | core | native_index | 4 | 4 | 128547 | 0.0 | 32136.8 | n/a |
| contradiction | core | none | 6 | 0 | 1740 | 0.0 | undefined | n/a |
| contradiction | core | oracle | 6 | 6 | 3063 | 0.0 | 510.5 | 3562 |
| contradiction | core | pull | 6 | 6 | 64518 | 0.0 | 10753.0 | n/a |
| contradiction | core | push_breadcrumb | 6 | 0 | 3167 | 0.0 | undefined | 3968 |
| contradiction | core | push_breadcrumb_pull | 6 | 6 | 82911 | 0.0 | 13818.5 | 3968 |
| contradiction | core | push_pages_upper_bound | 6 | 2 | 7617 | 0.0 | 3808.5 | 17506 |
| contradiction | medium | native_grep | 6 | 5 | 54414 | 0.0 | 10882.8 | n/a |
| contradiction | medium | native_index | 6 | 5 | 165936 | 0.0 | 33187.2 | n/a |
| contradiction | medium | none | 6 | 0 | 1864 | 0.0 | undefined | n/a |
| contradiction | medium | oracle | 6 | 6 | 3019 | 0.0 | 503.2 | 3562 |
| contradiction | medium | pull | 6 | 6 | 63373 | 0.0 | 10562.2 | n/a |
| contradiction | medium | push_breadcrumb | 6 | 0 | 3284 | 0.0 | undefined | 3982 |
| contradiction | medium | push_breadcrumb_pull | 6 | 6 | 73161 | 0.0 | 12193.5 | 3982 |
| contradiction | medium | push_pages_upper_bound | 6 | 2 | 7781 | 0.0 | 3890.5 | 18692 |
| disambiguation | core | native_grep | 8 | 5 | 43600 | 0.0 | 8720.0 | n/a |
| disambiguation | core | native_index | 8 | 5 | 196756 | 0.0 | 39351.2 | n/a |
| disambiguation | core | none | 8 | 0 | 2292 | 0.0 | undefined | n/a |
| disambiguation | core | oracle | 8 | 8 | 7469 | 0.0 | 933.6 | 13588 |
| disambiguation | core | pull | 8 | 8 | 60797 | 0.0 | 7599.6 | n/a |
| disambiguation | core | push_breadcrumb | 8 | 0 | 4247 | 0.0 | undefined | 4428 |
| disambiguation | core | push_breadcrumb_pull | 8 | 8 | 67729 | 0.0 | 8466.1 | 4428 |
| disambiguation | core | push_pages_upper_bound | 8 | 8 | 10579 | 0.0 | 1322.4 | 23994 |
| disambiguation | medium | native_grep | 8 | 5 | 56263 | 0.0 | 11252.6 | n/a |
| disambiguation | medium | native_index | 8 | 2 | 193669 | 0.0 | 96834.5 | n/a |
| disambiguation | medium | none | 8 | 0 | 2317 | 0.0 | undefined | n/a |
| disambiguation | medium | oracle | 8 | 8 | 7513 | 0.0 | 939.1 | 13588 |
| disambiguation | medium | pull | 8 | 8 | 84782 | 0.0 | 10597.8 | n/a |
| disambiguation | medium | push_breadcrumb | 8 | 0 | 4513 | 0.0 | undefined | 4882 |
| disambiguation | medium | push_breadcrumb_pull | 8 | 8 | 65378 | 0.0 | 8172.2 | 4882 |
| disambiguation | medium | push_pages_upper_bound | 8 | 8 | 10837 | 0.0 | 1354.6 | 24000 |
| distractor_robustness | core | native_grep | 4 | 1 | 27881 | 0.0 | 27881.0 | n/a |
| distractor_robustness | core | native_index | 4 | 0 | 86344 | 0.0 | undefined | n/a |
| distractor_robustness | core | none | 4 | 0 | 1115 | 0.0 | undefined | n/a |
| distractor_robustness | core | oracle | 4 | 4 | 2871 | 0.0 | 717.8 | 4752 |
| distractor_robustness | core | pull | 4 | 3 | 26951 | 0.0 | 8983.7 | n/a |
| distractor_robustness | core | push_breadcrumb | 4 | 0 | 2130 | 0.0 | undefined | 2320 |
| distractor_robustness | core | push_breadcrumb_pull | 4 | 4 | 29779 | 0.0 | 7444.8 | 2320 |
| distractor_robustness | core | push_pages_upper_bound | 4 | 4 | 4921 | 0.0 | 1230.2 | 11450 |
| distractor_robustness | medium | native_grep | 4 | 2 | 27897 | 0.0 | 13948.5 | n/a |
| distractor_robustness | medium | native_index | 4 | 1 | 102015 | 0.0 | 102015.0 | n/a |
| distractor_robustness | medium | none | 4 | 0 | 1113 | 0.0 | undefined | n/a |
| distractor_robustness | medium | oracle | 4 | 4 | 2879 | 0.0 | 719.8 | 4752 |
| distractor_robustness | medium | pull | 4 | 2 | 32852 | 0.0 | 16426.0 | n/a |
| distractor_robustness | medium | push_breadcrumb | 4 | 0 | 2135 | 0.0 | undefined | 2436 |
| distractor_robustness | medium | push_breadcrumb_pull | 4 | 4 | 47469 | 0.0 | 11867.2 | 2436 |
| distractor_robustness | medium | push_pages_upper_bound | 4 | 2 | 4490 | 0.0 | 2245.0 | 10636 |
| follow_through | core | native_grep | 11 | 9 | 65157 | 0.0 | 7239.7 | n/a |
| follow_through | core | native_index | 11 | 10 | 342458 | 0.0 | 34245.8 | n/a |
| follow_through | core | none | 12 | 0 | 3854 | 0.0 | undefined | n/a |
| follow_through | core | oracle | 12 | 12 | 8472 | 0.0 | 706.0 | 12546 |
| follow_through | core | pull | 12 | 12 | 221685 | 0.0 | 18473.8 | n/a |
| follow_through | core | push_breadcrumb | 12 | 0 | 6891 | 0.0 | undefined | 8008 |
| follow_through | core | push_breadcrumb_pull | 11 | 10 | 191875 | 0.0 | 19187.5 | 7251 |
| follow_through | core | push_pages_upper_bound | 12 | 0 | 14792 | 0.0 | undefined | 35352 |
| follow_through | medium | native_grep | 5 | 5 | 44133 | 0.0 | 8826.6 | n/a |
| follow_through | medium | native_index | 12 | 12 | 433225 | 0.0 | 36102.1 | n/a |
| follow_through | medium | none | 12 | 0 | 4050 | 0.0 | undefined | n/a |
| follow_through | medium | oracle | 12 | 12 | 8476 | 0.0 | 706.3 | 12546 |
| follow_through | medium | pull | 11 | 11 | 238164 | 0.0 | 21651.3 | n/a |
| follow_through | medium | push_breadcrumb | 12 | 0 | 7243 | 0.0 | undefined | 7770 |
| follow_through | medium | push_breadcrumb_pull | 12 | 11 | 287831 | 0.0 | 26166.5 | 7770 |
| follow_through | medium | push_pages_upper_bound | 12 | 0 | 15361 | 0.0 | undefined | 36012 |
| multi_hop | core | native_grep | 5 | 5 | 59078 | 0.0 | 11815.6 | n/a |
| multi_hop | core | native_index | 6 | 4 | 156510 | 0.0 | 39127.5 | n/a |
| multi_hop | core | none | 6 | 0 | 1849 | 0.0 | undefined | n/a |
| multi_hop | core | oracle | 6 | 5 | 4939 | 0.0 | 987.8 | 8158 |
| multi_hop | core | pull | 4 | 4 | 76880 | 0.0 | 19220.0 | n/a |
| multi_hop | core | push_breadcrumb | 6 | 0 | 3218 | 0.0 | undefined | 3492 |
| multi_hop | core | push_breadcrumb_pull | 5 | 4 | 134453 | 0.0 | 33613.2 | 2932 |
| multi_hop | core | push_pages_upper_bound | 6 | 0 | 7340 | 0.0 | undefined | 16930 |
| multi_hop | medium | native_grep | 3 | 2 | 39620 | 0.0 | 19810.0 | n/a |
| multi_hop | medium | native_index | 6 | 3 | 190510 | 0.0 | 63503.3 | n/a |
| multi_hop | medium | none | 6 | 0 | 1867 | 0.0 | undefined | n/a |
| multi_hop | medium | oracle | 6 | 5 | 4943 | 0.0 | 988.6 | 8158 |
| multi_hop | medium | pull | 3 | 2 | 65001 | 0.0 | 32500.5 | n/a |
| multi_hop | medium | push_breadcrumb | 6 | 0 | 3340 | 0.0 | undefined | 3666 |
| multi_hop | medium | push_breadcrumb_pull | 6 | 3 | 159639 | 0.0 | 53213.0 | 3666 |
| multi_hop | medium | push_pages_upper_bound | 6 | 0 | 7305 | 0.0 | undefined | 16578 |
| negative_knowledge | core | native_grep | 2 | 2 | 9025 | 0.0 | 4512.5 | n/a |
| negative_knowledge | core | native_index | 2 | 1 | 49899 | 0.0 | 49899.0 | n/a |
| negative_knowledge | core | none | 2 | 0 | 717 | 0.0 | undefined | n/a |
| negative_knowledge | core | oracle | 2 | 2 | 1104 | 0.0 | 552.0 | 1046 |
| negative_knowledge | core | pull | 2 | 2 | 20176 | 0.0 | 10088.0 | n/a |
| negative_knowledge | core | push_breadcrumb | 2 | 0 | 1239 | 0.0 | undefined | 1252 |
| negative_knowledge | core | push_breadcrumb_pull | 2 | 2 | 29945 | 0.0 | 14972.5 | 1252 |
| negative_knowledge | core | push_pages_upper_bound | 2 | 2 | 2372 | 0.0 | 1186.0 | 5640 |
| redundancy | core | native_grep | 8 | 2 | 33855 | 0.0 | 16927.5 | n/a |
| redundancy | core | native_index | 8 | 1 | 204950 | 0.0 | 204950.0 | n/a |
| redundancy | core | none | 8 | 0 | 2149 | 0.0 | undefined | n/a |
| redundancy | core | oracle | 8 | 4 | 5614 | 0.0 | 1403.5 | 10606 |
| redundancy | core | pull | 8 | 7 | 63049 | 0.0 | 9007.0 | n/a |
| redundancy | core | push_breadcrumb | 8 | 0 | 4330 | 0.0 | undefined | 5154 |
| redundancy | core | push_breadcrumb_pull | 8 | 6 | 63755 | 0.0 | 10625.8 | 5154 |
| redundancy | core | push_pages_upper_bound | 8 | 6 | 9292 | 0.0 | 1548.7 | 23706 |
| redundancy | medium | native_grep | 8 | 0 | 39260 | 0.0 | undefined | n/a |
| redundancy | medium | native_index | 8 | 1 | 230895 | 0.0 | 230895.0 | n/a |
| redundancy | medium | none | 8 | 0 | 2121 | 0.0 | undefined | n/a |
| redundancy | medium | oracle | 8 | 5 | 5606 | 0.0 | 1121.2 | 10606 |
| redundancy | medium | pull | 8 | 6 | 54642 | 0.0 | 9107.0 | n/a |
| redundancy | medium | push_breadcrumb | 8 | 0 | 4267 | 0.0 | undefined | 4964 |
| redundancy | medium | push_breadcrumb_pull | 8 | 7 | 62267 | 0.0 | 8895.3 | 4964 |
| redundancy | medium | push_pages_upper_bound | 8 | 7 | 9684 | 0.0 | 1383.4 | 24538 |
| single_hop | core | native_grep | 16 | 15 | 76624 | 0.0 | 5108.3 | n/a |
| single_hop | core | native_index | 16 | 13 | 369467 | 0.0 | 28420.5 | n/a |
| single_hop | core | none | 16 | 0 | 4585 | 0.0 | undefined | n/a |
| single_hop | core | oracle | 16 | 11 | 11507 | 0.0 | 1046.1 | 21708 |
| single_hop | core | pull | 16 | 14 | 131295 | 0.0 | 9378.2 | n/a |
| single_hop | core | push_breadcrumb | 16 | 0 | 8489 | 0.0 | undefined | 9868 |
| single_hop | core | push_breadcrumb_pull | 16 | 13 | 148353 | 0.0 | 11411.8 | 9868 |
| single_hop | core | push_pages_upper_bound | 16 | 6 | 18082 | 0.0 | 3013.7 | 43160 |
| single_hop | medium | native_grep | 16 | 14 | 84608 | 0.0 | 6043.4 | n/a |
| single_hop | medium | native_index | 16 | 14 | 406854 | 0.0 | 29061.0 | n/a |
| single_hop | medium | none | 16 | 0 | 4513 | 0.0 | undefined | n/a |
| single_hop | medium | oracle | 16 | 12 | 11481 | 0.0 | 956.8 | 21708 |
| single_hop | medium | pull | 16 | 15 | 130341 | 0.0 | 8689.4 | n/a |
| single_hop | medium | push_breadcrumb | 16 | 0 | 8702 | 0.0 | undefined | 10256 |
| single_hop | medium | push_breadcrumb_pull | 16 | 16 | 167930 | 0.0 | 10495.6 | 10256 |
| single_hop | medium | push_pages_upper_bound | 16 | 6 | 18931 | 0.0 | 3155.2 | 46954 |
| temporal | core | native_grep | 12 | 12 | 51667 | 0.0 | 4305.6 | n/a |
| temporal | core | native_index | 12 | 7 | 283938 | 0.0 | 40562.6 | n/a |
| temporal | core | none | 12 | 0 | 3139 | 0.0 | undefined | n/a |
| temporal | core | oracle | 12 | 12 | 5535 | 0.0 | 461.2 | 6464 |
| temporal | core | pull | 12 | 11 | 68250 | 0.0 | 6204.5 | n/a |
| temporal | core | push_breadcrumb | 12 | 0 | 6440 | 0.0 | undefined | 7314 |
| temporal | core | push_breadcrumb_pull | 12 | 12 | 85441 | 0.0 | 7120.1 | 7314 |
| temporal | core | push_pages_upper_bound | 12 | 12 | 13198 | 0.0 | 1099.8 | 30978 |
| temporal | medium | native_grep | 11 | 8 | 56245 | 0.0 | 7030.6 | n/a |
| temporal | medium | native_index | 12 | 8 | 308705 | 0.0 | 38588.1 | n/a |
| temporal | medium | none | 12 | 0 | 3179 | 0.0 | undefined | n/a |
| temporal | medium | oracle | 12 | 12 | 5482 | 0.0 | 456.8 | 6464 |
| temporal | medium | pull | 12 | 12 | 88428 | 0.0 | 7369.0 | n/a |
| temporal | medium | push_breadcrumb | 12 | 0 | 6473 | 0.0 | undefined | 7514 |
| temporal | medium | push_breadcrumb_pull | 12 | 12 | 92647 | 0.0 | 7720.6 | 7514 |
| temporal | medium | push_pages_upper_bound | 12 | 12 | 13634 | 0.0 | 1136.2 | 32280 |

## Query quality

`lexical_overlap(a, b) = |terms(a) ∩ terms(b)| / |terms(a) ∪ terms(b)|` (Jaccard) — `terms()` is lowercased alphanumeric tokens of length >= 3, minus a short stopword list; denominator is the union of both term sets, 0.0 when either side is empty. `self_query_overlap` is measured between the agent's own recall-tool query and the target (ground-truth) page text; `topic_query_overlap` is the SAME formula against the same target, but using the probe's original query text verbatim as a zero-cost baseline (no extraction step is run to produce it — it already exists).

| probe_class | corpus_scale | n | self_query_overlap | topic_query_overlap |
| --- | --- | --- | --- | --- |
| abstention | core | 4 | 0.000 | 0.000 |
| abstention | medium | 4 | 0.000 | 0.000 |
| aggregation | core | 6 | 0.032 | 0.045 |
| aggregation | medium | 5 | 0.029 | 0.041 |
| contradiction | core | 6 | 0.072 | 0.019 |
| contradiction | medium | 6 | 0.077 | 0.019 |
| disambiguation | core | 8 | 0.032 | 0.025 |
| disambiguation | medium | 8 | 0.032 | 0.025 |
| distractor_robustness | core | 4 | 0.032 | 0.030 |
| distractor_robustness | medium | 4 | 0.024 | 0.030 |
| follow_through | core | 12 | 0.060 | 0.054 |
| follow_through | medium | 11 | 0.057 | 0.056 |
| multi_hop | core | 4 | 0.057 | 0.053 |
| multi_hop | medium | 3 | 0.056 | 0.053 |
| negative_knowledge | core | 2 | 0.108 | 0.078 |
| redundancy | core | 8 | 0.043 | 0.051 |
| redundancy | medium | 8 | 0.040 | 0.051 |
| single_hop | core | 16 | 0.045 | 0.057 |
| single_hop | medium | 16 | 0.042 | 0.057 |
| temporal | core | 12 | 0.069 | 0.068 |
| temporal | medium | 12 | 0.074 | 0.068 |

## Cost (per turn, not per task)

`mean_injected_context_tokens` (PUSH/ORACLE) is counted whether the delivered pages were used or not — that is the asymmetry against PULL, which pays only on a turn where it actually calls recall. Read the two columns side by side, never in isolation.

| probe_class | corpus_scale | arm | n | mean_input_tokens/turn | mean_output_tokens/turn | mean_injected_context_tokens |
| --- | --- | --- | --- | --- | --- | --- |
| abstention | core | native_grep | 3 | 1344.9 | 81.8 | n/a |
| abstention | core | native_index | 6 | 5838.8 | 74.3 | n/a |
| abstention | core | none | 6 | 213.7 | 91.0 | n/a |
| abstention | core | oracle | 6 | 213.7 | 62.3 | 0.0 |
| abstention | core | pull | 4 | 5450.9 | 105.6 | n/a |
| abstention | core | push_breadcrumb | 6 | 388.3 | 104.7 | 143.3 |
| abstention | core | push_breadcrumb_pull | 6 | 6395.5 | 119.4 | 143.3 |
| abstention | core | push_pages_upper_bound | 6 | 1092.7 | 68.5 | 731.0 |
| abstention | medium | native_grep | 4 | 1289.3 | 99.7 | n/a |
| abstention | medium | native_index | 6 | 6362.3 | 105.8 | n/a |
| abstention | medium | none | 6 | 213.7 | 75.3 | n/a |
| abstention | medium | oracle | 6 | 213.7 | 71.3 | 0.0 |
| abstention | medium | pull | 4 | 5122.8 | 112.3 | n/a |
| abstention | medium | push_breadcrumb | 6 | 390.7 | 143.3 | 150.7 |
| abstention | medium | push_breadcrumb_pull | 5 | 5231.3 | 121.7 | 146.8 |
| abstention | medium | push_pages_upper_bound | 6 | 1058.0 | 127.0 | 711.0 |
| aggregation | core | native_grep | 5 | 3458.0 | 226.1 | n/a |
| aggregation | core | native_index | 6 | 6362.4 | 185.8 | n/a |
| aggregation | core | none | 6 | 218.0 | 77.2 | n/a |
| aggregation | core | oracle | 6 | 1304.0 | 254.3 | 770.7 |
| aggregation | core | pull | 6 | 4693.9 | 173.2 | n/a |
| aggregation | core | push_breadcrumb | 6 | 395.7 | 162.0 | 144.7 |
| aggregation | core | push_breadcrumb_pull | 6 | 4515.1 | 248.1 | 144.7 |
| aggregation | core | push_pages_upper_bound | 6 | 1004.0 | 159.5 | 625.0 |
| aggregation | medium | native_grep | 2 | 3020.1 | 213.4 | n/a |
| aggregation | medium | native_index | 6 | 8612.6 | 168.2 | n/a |
| aggregation | medium | none | 6 | 218.0 | 88.7 | n/a |
| aggregation | medium | oracle | 6 | 1304.0 | 243.0 | 770.7 |
| aggregation | medium | pull | 5 | 5993.8 | 189.5 | n/a |
| aggregation | medium | push_breadcrumb | 6 | 396.7 | 170.5 | 155.7 |
| aggregation | medium | push_breadcrumb_pull | 6 | 5342.2 | 192.7 | 155.7 |
| aggregation | medium | push_pages_upper_bound | 6 | 1035.3 | 186.7 | 688.0 |
| contradiction | core | native_grep | 6 | 1324.6 | 108.9 | n/a |
| contradiction | core | native_index | 4 | 7049.2 | 92.3 | n/a |
| contradiction | core | none | 6 | 216.0 | 74.0 | n/a |
| contradiction | core | oracle | 6 | 401.3 | 109.2 | 148.3 |
| contradiction | core | pull | 6 | 3475.1 | 109.2 | n/a |
| contradiction | core | push_breadcrumb | 6 | 398.3 | 129.5 | 164.7 |
| contradiction | core | push_breadcrumb_pull | 6 | 4026.6 | 119.0 | 164.7 |
| contradiction | core | push_pages_upper_bound | 6 | 1104.7 | 164.8 | 725.3 |
| contradiction | medium | native_grep | 6 | 2055.7 | 120.9 | n/a |
| contradiction | medium | native_index | 6 | 6547.2 | 90.2 | n/a |
| contradiction | medium | none | 6 | 216.0 | 94.7 | n/a |
| contradiction | medium | oracle | 6 | 401.3 | 101.8 | 148.3 |
| contradiction | medium | pull | 6 | 3407.7 | 113.0 | n/a |
| contradiction | medium | push_breadcrumb | 6 | 397.0 | 150.3 | 164.7 |
| contradiction | medium | push_breadcrumb_pull | 6 | 3940.6 | 123.9 | 164.7 |
| contradiction | medium | push_pages_upper_bound | 6 | 1111.0 | 185.8 | 774.7 |
| disambiguation | core | native_grep | 8 | 1696.2 | 120.5 | n/a |
| disambiguation | core | native_index | 8 | 6438.6 | 119.9 | n/a |
| disambiguation | core | none | 8 | 212.2 | 74.2 | n/a |
| disambiguation | core | oracle | 8 | 779.0 | 154.6 | 424.2 |
| disambiguation | core | pull | 8 | 3066.7 | 133.2 | n/a |
| disambiguation | core | push_breadcrumb | 8 | 395.5 | 135.4 | 137.5 |
| disambiguation | core | push_breadcrumb_pull | 8 | 3611.8 | 150.9 | 137.5 |
| disambiguation | core | push_pages_upper_bound | 8 | 1151.5 | 170.9 | 746.5 |
| disambiguation | medium | native_grep | 8 | 2049.5 | 114.5 | n/a |
| disambiguation | medium | native_index | 8 | 6807.4 | 109.3 | n/a |
| disambiguation | medium | none | 8 | 212.2 | 77.4 | n/a |
| disambiguation | medium | oracle | 8 | 779.0 | 160.1 | 424.2 |
| disambiguation | medium | pull | 8 | 3560.5 | 125.7 | n/a |
| disambiguation | medium | push_breadcrumb | 8 | 404.0 | 160.1 | 151.5 |
| disambiguation | medium | push_breadcrumb_pull | 8 | 3678.5 | 167.3 | 151.5 |
| disambiguation | medium | push_pages_upper_bound | 8 | 1165.2 | 189.4 | 746.8 |
| distractor_robustness | core | native_grep | 4 | 2115.5 | 207.9 | n/a |
| distractor_robustness | core | native_index | 4 | 6507.8 | 134.0 | n/a |
| distractor_robustness | core | none | 4 | 210.0 | 68.8 | n/a |
| distractor_robustness | core | oracle | 4 | 621.0 | 96.8 | 296.5 |
| distractor_robustness | core | pull | 4 | 2880.9 | 113.7 | n/a |
| distractor_robustness | core | push_breadcrumb | 4 | 393.5 | 139.0 | 144.0 |
| distractor_robustness | core | push_breadcrumb_pull | 4 | 3564.5 | 157.9 | 144.0 |
| distractor_robustness | core | push_pages_upper_bound | 4 | 1068.0 | 162.2 | 711.5 |
| distractor_robustness | medium | native_grep | 4 | 2109.6 | 215.2 | n/a |
| distractor_robustness | medium | native_index | 4 | 7152.0 | 134.8 | n/a |
| distractor_robustness | medium | none | 4 | 210.0 | 68.2 | n/a |
| distractor_robustness | medium | oracle | 4 | 621.0 | 98.8 | 296.5 |
| distractor_robustness | medium | pull | 4 | 3173.2 | 112.0 | n/a |
| distractor_robustness | medium | push_breadcrumb | 4 | 387.5 | 146.2 | 151.0 |
| distractor_robustness | medium | push_breadcrumb_pull | 4 | 4162.2 | 153.2 | 151.0 |
| distractor_robustness | medium | push_pages_upper_bound | 4 | 1011.5 | 111.0 | 661.5 |
| follow_through | core | native_grep | 11 | 1368.7 | 112.2 | n/a |
| follow_through | core | native_index | 11 | 6243.9 | 97.9 | n/a |
| follow_through | core | none | 12 | 229.0 | 92.2 | n/a |
| follow_through | core | oracle | 12 | 567.2 | 138.8 | 261.3 |
| follow_through | core | pull | 12 | 4382.5 | 141.7 | n/a |
| follow_through | core | push_breadcrumb | 12 | 417.0 | 157.2 | 165.8 |
| follow_through | core | push_breadcrumb_pull | 11 | 5015.6 | 170.2 | 163.8 |
| follow_through | core | push_pages_upper_bound | 12 | 1087.5 | 145.2 | 733.0 |
| follow_through | medium | native_grep | 5 | 1880.5 | 125.5 | n/a |
| follow_through | medium | native_index | 12 | 6995.5 | 106.6 | n/a |
| follow_through | medium | none | 12 | 229.0 | 108.5 | n/a |
| follow_through | medium | oracle | 12 | 567.2 | 139.2 | 261.3 |
| follow_through | medium | pull | 11 | 5245.1 | 167.8 | n/a |
| follow_through | medium | push_breadcrumb | 12 | 413.3 | 190.2 | 160.5 |
| follow_through | medium | push_breadcrumb_pull | 12 | 5945.8 | 178.2 | 160.5 |
| follow_through | medium | push_pages_upper_bound | 12 | 1100.8 | 179.2 | 746.8 |
| multi_hop | core | native_grep | 5 | 2144.5 | 127.7 | n/a |
| multi_hop | core | native_index | 6 | 6665.1 | 139.7 | n/a |
| multi_hop | core | none | 6 | 231.7 | 76.5 | n/a |
| multi_hop | core | oracle | 6 | 690.7 | 132.5 | 339.3 |
| multi_hop | core | pull | 4 | 4401.9 | 120.4 | n/a |
| multi_hop | core | push_breadcrumb | 6 | 404.0 | 132.3 | 144.7 |
| multi_hop | core | push_breadcrumb_pull | 5 | 6249.5 | 153.0 | 145.8 |
| multi_hop | core | push_pages_upper_bound | 6 | 1061.0 | 162.3 | 702.3 |
| multi_hop | medium | native_grep | 3 | 2493.3 | 148.0 | n/a |
| multi_hop | medium | native_index | 6 | 7192.5 | 134.8 | n/a |
| multi_hop | medium | none | 6 | 231.7 | 79.5 | n/a |
| multi_hop | medium | oracle | 6 | 690.7 | 133.2 | 339.3 |
| multi_hop | medium | pull | 3 | 4862.9 | 137.2 | n/a |
| multi_hop | medium | push_breadcrumb | 6 | 405.3 | 151.3 | 151.7 |
| multi_hop | medium | push_breadcrumb_pull | 6 | 6218.4 | 167.1 | 151.7 |
| multi_hop | medium | push_pages_upper_bound | 6 | 1075.7 | 141.8 | 688.0 |
| negative_knowledge | core | native_grep | 2 | 1404.7 | 99.5 | n/a |
| negative_knowledge | core | native_index | 2 | 6142.9 | 94.5 | n/a |
| negative_knowledge | core | none | 2 | 224.0 | 134.5 | n/a |
| negative_knowledge | core | oracle | 2 | 384.0 | 168.0 | 130.0 |
| negative_knowledge | core | pull | 2 | 3194.2 | 168.5 | n/a |
| negative_knowledge | core | push_breadcrumb | 2 | 405.0 | 214.5 | 156.0 |
| negative_knowledge | core | push_breadcrumb_pull | 2 | 4144.0 | 133.9 | 156.0 |
| negative_knowledge | core | push_pages_upper_bound | 2 | 1061.0 | 125.0 | 702.0 |
| redundancy | core | native_grep | 8 | 1268.8 | 85.4 | n/a |
| redundancy | core | native_index | 8 | 6138.5 | 72.1 | n/a |
| redundancy | core | none | 8 | 214.8 | 53.9 | n/a |
| redundancy | core | oracle | 8 | 628.2 | 73.5 | 330.8 |
| redundancy | core | pull | 8 | 3061.3 | 91.2 | n/a |
| redundancy | core | push_breadcrumb | 8 | 403.8 | 137.5 | 160.2 |
| redundancy | core | push_breadcrumb_pull | 8 | 3432.6 | 109.4 | 160.2 |
| redundancy | core | push_pages_upper_bound | 8 | 1083.5 | 78.0 | 737.8 |
| redundancy | medium | native_grep | 8 | 1479.1 | 91.3 | n/a |
| redundancy | medium | native_index | 8 | 6704.5 | 86.5 | n/a |
| redundancy | medium | none | 8 | 214.8 | 50.4 | n/a |
| redundancy | medium | oracle | 8 | 628.2 | 72.5 | 330.8 |
| redundancy | medium | pull | 8 | 2947.1 | 88.6 | n/a |
| redundancy | medium | push_breadcrumb | 8 | 397.5 | 135.9 | 154.2 |
| redundancy | medium | push_breadcrumb_pull | 8 | 3349.8 | 109.5 | 154.2 |
| redundancy | medium | push_pages_upper_bound | 8 | 1119.5 | 91.0 | 763.2 |
| single_hop | core | native_grep | 16 | 1377.8 | 124.6 | n/a |
| single_hop | core | native_index | 16 | 6062.0 | 95.8 | n/a |
| single_hop | core | none | 16 | 215.0 | 71.6 | n/a |
| single_hop | core | oracle | 16 | 566.5 | 152.7 | 338.8 |
| single_hop | core | pull | 16 | 3139.1 | 143.3 | n/a |
| single_hop | core | push_breadcrumb | 16 | 397.6 | 132.9 | 153.2 |
| single_hop | core | push_breadcrumb_pull | 16 | 3558.2 | 150.7 | 153.2 |
| single_hop | core | push_pages_upper_bound | 16 | 1027.4 | 102.8 | 671.0 |
| single_hop | medium | native_grep | 16 | 1563.1 | 129.1 | n/a |
| single_hop | medium | native_index | 16 | 6465.2 | 97.0 | n/a |
| single_hop | medium | none | 16 | 215.0 | 67.1 | n/a |
| single_hop | medium | oracle | 16 | 566.5 | 151.1 | 338.8 |
| single_hop | medium | pull | 16 | 3115.2 | 143.3 | n/a |
| single_hop | medium | push_breadcrumb | 16 | 399.0 | 144.9 | 159.0 |
| single_hop | medium | push_breadcrumb_pull | 16 | 3844.2 | 154.1 | 159.0 |
| single_hop | medium | push_pages_upper_bound | 16 | 1072.5 | 110.7 | 730.4 |
| temporal | core | native_grep | 12 | 1354.4 | 80.8 | n/a |
| temporal | core | native_index | 12 | 6103.3 | 69.3 | n/a |
| temporal | core | none | 12 | 212.3 | 49.2 | n/a |
| temporal | core | oracle | 12 | 399.2 | 62.1 | 134.3 |
| temporal | core | pull | 12 | 2762.4 | 81.4 | n/a |
| temporal | core | push_breadcrumb | 12 | 399.2 | 137.5 | 151.7 |
| temporal | core | push_breadcrumb_pull | 12 | 3187.3 | 98.9 | 151.7 |
| temporal | core | push_pages_upper_bound | 12 | 1028.8 | 71.0 | 642.3 |
| temporal | medium | native_grep | 11 | 1569.8 | 84.5 | n/a |
| temporal | medium | native_index | 12 | 6493.6 | 74.6 | n/a |
| temporal | medium | none | 12 | 212.3 | 52.6 | n/a |
| temporal | medium | oracle | 12 | 399.2 | 57.7 | 134.3 |
| temporal | medium | pull | 12 | 2964.7 | 84.5 | n/a |
| temporal | medium | push_breadcrumb | 12 | 394.8 | 144.6 | 155.3 |
| temporal | medium | push_breadcrumb_pull | 12 | 3335.6 | 95.7 | 155.3 |
| temporal | medium | push_pages_upper_bound | 12 | 1047.7 | 88.5 | 669.7 |

## Efficiency

| probe_class | corpus_scale | arm | n | mean_turn_count | mean_tool_call_count |
| --- | --- | --- | --- | --- | --- |
| abstention | core | native_grep | 3 | 4.00 | 4.33 |
| abstention | core | native_index | 6 | 2.00 | 1.67 |
| abstention | core | none | 6 | 1.00 | 0.00 |
| abstention | core | oracle | 6 | 1.00 | 0.00 |
| abstention | core | pull | 4 | 5.00 | 4.00 |
| abstention | core | push_breadcrumb | 6 | 1.00 | 0.00 |
| abstention | core | push_breadcrumb_pull | 6 | 5.50 | 4.50 |
| abstention | core | push_pages_upper_bound | 6 | 1.00 | 0.00 |
| abstention | medium | native_grep | 4 | 3.75 | 3.50 |
| abstention | medium | native_index | 6 | 2.50 | 2.50 |
| abstention | medium | none | 6 | 1.00 | 0.00 |
| abstention | medium | oracle | 6 | 1.00 | 0.00 |
| abstention | medium | pull | 4 | 4.75 | 3.75 |
| abstention | medium | push_breadcrumb | 6 | 1.00 | 0.00 |
| abstention | medium | push_breadcrumb_pull | 5 | 4.40 | 3.60 |
| abstention | medium | push_pages_upper_bound | 6 | 1.00 | 0.00 |
| aggregation | core | native_grep | 5 | 4.40 | 12.80 |
| aggregation | core | native_index | 6 | 2.17 | 1.67 |
| aggregation | core | none | 6 | 1.00 | 0.00 |
| aggregation | core | oracle | 6 | 1.00 | 0.00 |
| aggregation | core | pull | 6 | 3.17 | 3.17 |
| aggregation | core | push_breadcrumb | 6 | 1.00 | 0.00 |
| aggregation | core | push_breadcrumb_pull | 6 | 2.33 | 3.33 |
| aggregation | core | push_pages_upper_bound | 6 | 1.00 | 0.00 |
| aggregation | medium | native_grep | 2 | 3.50 | 9.00 |
| aggregation | medium | native_index | 6 | 3.50 | 4.67 |
| aggregation | medium | none | 6 | 1.00 | 0.00 |
| aggregation | medium | oracle | 6 | 1.00 | 0.00 |
| aggregation | medium | pull | 5 | 4.60 | 7.20 |
| aggregation | medium | push_breadcrumb | 6 | 1.00 | 0.00 |
| aggregation | medium | push_breadcrumb_pull | 6 | 3.33 | 3.00 |
| aggregation | medium | push_pages_upper_bound | 6 | 1.00 | 0.00 |
| contradiction | core | native_grep | 6 | 3.67 | 4.33 |
| contradiction | core | native_index | 4 | 4.50 | 4.75 |
| contradiction | core | none | 6 | 1.00 | 0.00 |
| contradiction | core | oracle | 6 | 1.00 | 0.00 |
| contradiction | core | pull | 6 | 3.00 | 2.33 |
| contradiction | core | push_breadcrumb | 6 | 1.00 | 0.00 |
| contradiction | core | push_breadcrumb_pull | 6 | 3.33 | 2.50 |
| contradiction | core | push_pages_upper_bound | 6 | 1.00 | 0.00 |
| contradiction | medium | native_grep | 6 | 4.17 | 6.00 |
| contradiction | medium | native_index | 6 | 4.17 | 3.83 |
| contradiction | medium | none | 6 | 1.00 | 0.00 |
| contradiction | medium | oracle | 6 | 1.00 | 0.00 |
| contradiction | medium | pull | 6 | 3.00 | 2.33 |
| contradiction | medium | push_breadcrumb | 6 | 1.00 | 0.00 |
| contradiction | medium | push_breadcrumb_pull | 6 | 3.00 | 2.33 |
| contradiction | medium | push_pages_upper_bound | 6 | 1.00 | 0.00 |
| disambiguation | core | native_grep | 8 | 3.00 | 3.12 |
| disambiguation | core | native_index | 8 | 3.75 | 4.75 |
| disambiguation | core | none | 8 | 1.00 | 0.00 |
| disambiguation | core | oracle | 8 | 1.00 | 0.00 |
| disambiguation | core | pull | 8 | 2.38 | 1.50 |
| disambiguation | core | push_breadcrumb | 8 | 1.00 | 0.00 |
| disambiguation | core | push_breadcrumb_pull | 8 | 2.25 | 1.62 |
| disambiguation | core | push_pages_upper_bound | 8 | 1.00 | 0.00 |
| disambiguation | medium | native_grep | 8 | 3.25 | 2.88 |
| disambiguation | medium | native_index | 8 | 3.50 | 3.50 |
| disambiguation | medium | none | 8 | 1.00 | 0.00 |
| disambiguation | medium | oracle | 8 | 1.00 | 0.00 |
| disambiguation | medium | pull | 8 | 2.88 | 2.00 |
| disambiguation | medium | push_breadcrumb | 8 | 1.00 | 0.00 |
| disambiguation | medium | push_breadcrumb_pull | 8 | 2.12 | 1.25 |
| disambiguation | medium | push_pages_upper_bound | 8 | 1.00 | 0.00 |
| distractor_robustness | core | native_grep | 4 | 3.00 | 8.25 |
| distractor_robustness | core | native_index | 4 | 3.25 | 3.00 |
| distractor_robustness | core | none | 4 | 1.00 | 0.00 |
| distractor_robustness | core | oracle | 4 | 1.00 | 0.00 |
| distractor_robustness | core | pull | 4 | 2.25 | 1.25 |
| distractor_robustness | core | push_breadcrumb | 4 | 1.00 | 0.00 |
| distractor_robustness | core | push_breadcrumb_pull | 4 | 2.00 | 1.00 |
| distractor_robustness | core | push_pages_upper_bound | 4 | 1.00 | 0.00 |
| distractor_robustness | medium | native_grep | 4 | 3.00 | 8.75 |
| distractor_robustness | medium | native_index | 4 | 3.50 | 3.50 |
| distractor_robustness | medium | none | 4 | 1.00 | 0.00 |
| distractor_robustness | medium | oracle | 4 | 1.00 | 0.00 |
| distractor_robustness | medium | pull | 4 | 2.50 | 2.00 |
| distractor_robustness | medium | push_breadcrumb | 4 | 1.00 | 0.00 |
| distractor_robustness | medium | push_breadcrumb_pull | 4 | 2.75 | 2.50 |
| distractor_robustness | medium | push_pages_upper_bound | 4 | 1.00 | 0.00 |
| follow_through | core | native_grep | 11 | 4.00 | 4.18 |
| follow_through | core | native_index | 11 | 4.91 | 4.82 |
| follow_through | core | none | 12 | 1.00 | 0.00 |
| follow_through | core | oracle | 12 | 1.00 | 0.00 |
| follow_through | core | pull | 12 | 4.08 | 4.33 |
| follow_through | core | push_breadcrumb | 12 | 1.00 | 0.00 |
| follow_through | core | push_breadcrumb_pull | 11 | 3.36 | 3.82 |
| follow_through | core | push_pages_upper_bound | 12 | 1.00 | 0.00 |
| follow_through | medium | native_grep | 5 | 4.40 | 5.80 |
| follow_through | medium | native_index | 12 | 5.08 | 5.92 |
| follow_through | medium | none | 12 | 1.00 | 0.00 |
| follow_through | medium | oracle | 12 | 1.00 | 0.00 |
| follow_through | medium | pull | 11 | 4.00 | 5.45 |
| follow_through | medium | push_breadcrumb | 12 | 1.00 | 0.00 |
| follow_through | medium | push_breadcrumb_pull | 12 | 3.92 | 5.00 |
| follow_through | medium | push_pages_upper_bound | 12 | 1.00 | 0.00 |
| multi_hop | core | native_grep | 5 | 5.20 | 9.00 |
| multi_hop | core | native_index | 6 | 3.83 | 6.33 |
| multi_hop | core | none | 6 | 1.00 | 0.00 |
| multi_hop | core | oracle | 6 | 1.00 | 0.00 |
| multi_hop | core | pull | 4 | 4.25 | 4.25 |
| multi_hop | core | push_breadcrumb | 6 | 1.00 | 0.00 |
| multi_hop | core | push_breadcrumb_pull | 5 | 4.20 | 5.00 |
| multi_hop | core | push_pages_upper_bound | 6 | 1.00 | 0.00 |
| multi_hop | medium | native_grep | 3 | 5.00 | 9.67 |
| multi_hop | medium | native_index | 6 | 4.33 | 7.00 |
| multi_hop | medium | none | 6 | 1.00 | 0.00 |
| multi_hop | medium | oracle | 6 | 1.00 | 0.00 |
| multi_hop | medium | pull | 3 | 4.33 | 5.33 |
| multi_hop | medium | push_breadcrumb | 6 | 1.00 | 0.00 |
| multi_hop | medium | push_breadcrumb_pull | 6 | 4.17 | 5.50 |
| multi_hop | medium | push_pages_upper_bound | 6 | 1.00 | 0.00 |
| negative_knowledge | core | native_grep | 2 | 3.00 | 2.50 |
| negative_knowledge | core | native_index | 2 | 4.00 | 4.50 |
| negative_knowledge | core | none | 2 | 1.00 | 0.00 |
| negative_knowledge | core | oracle | 2 | 1.00 | 0.00 |
| negative_knowledge | core | pull | 2 | 3.00 | 2.00 |
| negative_knowledge | core | push_breadcrumb | 2 | 1.00 | 0.00 |
| negative_knowledge | core | push_breadcrumb_pull | 2 | 3.50 | 2.50 |
| negative_knowledge | core | push_pages_upper_bound | 2 | 1.00 | 0.00 |
| redundancy | core | native_grep | 8 | 3.12 | 2.75 |
| redundancy | core | native_index | 8 | 4.12 | 3.50 |
| redundancy | core | none | 8 | 1.00 | 0.00 |
| redundancy | core | oracle | 8 | 1.00 | 0.00 |
| redundancy | core | pull | 8 | 2.50 | 1.50 |
| redundancy | core | push_breadcrumb | 8 | 1.00 | 0.00 |
| redundancy | core | push_breadcrumb_pull | 8 | 2.25 | 1.25 |
| redundancy | core | push_pages_upper_bound | 8 | 1.00 | 0.00 |
| redundancy | medium | native_grep | 8 | 3.12 | 3.00 |
| redundancy | medium | native_index | 8 | 4.25 | 4.62 |
| redundancy | medium | none | 8 | 1.00 | 0.00 |
| redundancy | medium | oracle | 8 | 1.00 | 0.00 |
| redundancy | medium | pull | 8 | 2.25 | 1.25 |
| redundancy | medium | push_breadcrumb | 8 | 1.00 | 0.00 |
| redundancy | medium | push_breadcrumb_pull | 8 | 2.25 | 1.25 |
| redundancy | medium | push_pages_upper_bound | 8 | 1.00 | 0.00 |
| single_hop | core | native_grep | 16 | 3.19 | 3.81 |
| single_hop | core | native_index | 16 | 3.75 | 3.06 |
| single_hop | core | none | 16 | 1.00 | 0.00 |
| single_hop | core | oracle | 16 | 1.00 | 0.00 |
| single_hop | core | pull | 16 | 2.50 | 1.69 |
| single_hop | core | push_breadcrumb | 16 | 1.00 | 0.00 |
| single_hop | core | push_breadcrumb_pull | 16 | 2.50 | 1.69 |
| single_hop | core | push_pages_upper_bound | 16 | 1.00 | 0.00 |
| single_hop | medium | native_grep | 16 | 3.12 | 3.56 |
| single_hop | medium | native_index | 16 | 3.88 | 3.25 |
| single_hop | medium | none | 16 | 1.00 | 0.00 |
| single_hop | medium | oracle | 16 | 1.00 | 0.00 |
| single_hop | medium | pull | 16 | 2.50 | 1.56 |
| single_hop | medium | push_breadcrumb | 16 | 1.00 | 0.00 |
| single_hop | medium | push_breadcrumb_pull | 16 | 2.62 | 1.81 |
| single_hop | medium | push_pages_upper_bound | 16 | 1.00 | 0.00 |
| temporal | core | native_grep | 12 | 3.00 | 2.67 |
| temporal | core | native_index | 12 | 3.83 | 3.25 |
| temporal | core | none | 12 | 1.00 | 0.00 |
| temporal | core | oracle | 12 | 1.00 | 0.00 |
| temporal | core | pull | 12 | 2.00 | 1.00 |
| temporal | core | push_breadcrumb | 12 | 1.00 | 0.00 |
| temporal | core | push_breadcrumb_pull | 12 | 2.17 | 1.42 |
| temporal | core | push_pages_upper_bound | 12 | 1.00 | 0.00 |
| temporal | medium | native_grep | 11 | 3.09 | 3.09 |
| temporal | medium | native_index | 12 | 3.92 | 3.50 |
| temporal | medium | none | 12 | 1.00 | 0.00 |
| temporal | medium | oracle | 12 | 1.00 | 0.00 |
| temporal | medium | pull | 12 | 2.42 | 1.42 |
| temporal | medium | push_breadcrumb | 12 | 1.00 | 0.00 |
| temporal | medium | push_breadcrumb_pull | 12 | 2.25 | 1.42 |
| temporal | medium | push_pages_upper_bound | 12 | 1.00 | 0.00 |

## Waste

`wasted_page_fraction = 1 - uid_citation_rate` — the fraction of delivered pages whose uid never appears in the answer text, i.e. never referenced anywhere later in the rollout. `wasted_tokens_estimate` distributes the delivered content's estimated token count evenly across delivered uids and sums the wasted share. `n/a` means nothing was delivered for that group (no basis to call anything wasted).

| probe_class | corpus_scale | arm | n | wasted_page_fraction | wasted_tokens_estimate |
| --- | --- | --- | --- | --- | --- |
| abstention | core | native_grep | 3 | n/a | n/a |
| abstention | core | native_index | 6 | n/a | n/a |
| abstention | core | none | 6 | n/a | n/a |
| abstention | core | oracle | 6 | n/a | n/a |
| abstention | core | pull | 4 | 1.000 | 5038.0 |
| abstention | core | push_breadcrumb | 6 | n/a | n/a |
| abstention | core | push_breadcrumb_pull | 6 | 1.000 | 6385.2 |
| abstention | core | push_pages_upper_bound | 6 | 1.000 | 731.0 |
| abstention | medium | native_grep | 4 | n/a | n/a |
| abstention | medium | native_index | 6 | n/a | n/a |
| abstention | medium | none | 6 | n/a | n/a |
| abstention | medium | oracle | 6 | n/a | n/a |
| abstention | medium | pull | 4 | 1.000 | 3879.8 |
| abstention | medium | push_breadcrumb | 6 | n/a | n/a |
| abstention | medium | push_breadcrumb_pull | 5 | 0.980 | 4104.0 |
| abstention | medium | push_pages_upper_bound | 6 | 0.933 | 661.3 |
| aggregation | core | native_grep | 5 | n/a | n/a |
| aggregation | core | native_index | 6 | n/a | n/a |
| aggregation | core | none | 6 | n/a | n/a |
| aggregation | core | oracle | 6 | 1.000 | 770.7 |
| aggregation | core | pull | 6 | 1.000 | 2883.5 |
| aggregation | core | push_breadcrumb | 6 | n/a | n/a |
| aggregation | core | push_breadcrumb_pull | 6 | 1.000 | 2952.8 |
| aggregation | core | push_pages_upper_bound | 6 | 1.000 | 625.0 |
| aggregation | medium | native_grep | 2 | n/a | n/a |
| aggregation | medium | native_index | 6 | n/a | n/a |
| aggregation | medium | none | 6 | n/a | n/a |
| aggregation | medium | oracle | 6 | 1.000 | 770.7 |
| aggregation | medium | pull | 5 | 1.000 | 5134.0 |
| aggregation | medium | push_breadcrumb | 6 | n/a | n/a |
| aggregation | medium | push_breadcrumb_pull | 6 | 1.000 | 3829.0 |
| aggregation | medium | push_pages_upper_bound | 6 | 0.967 | 665.0 |
| contradiction | core | native_grep | 6 | n/a | n/a |
| contradiction | core | native_index | 4 | n/a | n/a |
| contradiction | core | none | 6 | n/a | n/a |
| contradiction | core | oracle | 6 | 1.000 | 148.3 |
| contradiction | core | pull | 6 | 1.000 | 1570.3 |
| contradiction | core | push_breadcrumb | 6 | n/a | n/a |
| contradiction | core | push_breadcrumb_pull | 6 | 0.983 | 2081.6 |
| contradiction | core | push_pages_upper_bound | 6 | 1.000 | 725.3 |
| contradiction | medium | native_grep | 6 | n/a | n/a |
| contradiction | medium | native_index | 6 | n/a | n/a |
| contradiction | medium | none | 6 | n/a | n/a |
| contradiction | medium | oracle | 6 | 1.000 | 148.3 |
| contradiction | medium | pull | 6 | 1.000 | 1524.0 |
| contradiction | medium | push_breadcrumb | 6 | n/a | n/a |
| contradiction | medium | push_breadcrumb_pull | 6 | 1.000 | 2165.2 |
| contradiction | medium | push_pages_upper_bound | 6 | 0.833 | 648.6 |
| disambiguation | core | native_grep | 8 | n/a | n/a |
| disambiguation | core | native_index | 8 | n/a | n/a |
| disambiguation | core | none | 8 | n/a | n/a |
| disambiguation | core | oracle | 8 | 1.000 | 424.2 |
| disambiguation | core | pull | 8 | 0.950 | 934.3 |
| disambiguation | core | push_breadcrumb | 8 | n/a | n/a |
| disambiguation | core | push_breadcrumb_pull | 8 | 1.000 | 1631.2 |
| disambiguation | core | push_pages_upper_bound | 8 | 0.750 | 556.5 |
| disambiguation | medium | native_grep | 8 | n/a | n/a |
| disambiguation | medium | native_index | 8 | n/a | n/a |
| disambiguation | medium | none | 8 | n/a | n/a |
| disambiguation | medium | oracle | 8 | 1.000 | 424.2 |
| disambiguation | medium | pull | 8 | 1.000 | 1464.0 |
| disambiguation | medium | push_breadcrumb | 8 | n/a | n/a |
| disambiguation | medium | push_breadcrumb_pull | 8 | 0.912 | 1616.7 |
| disambiguation | medium | push_pages_upper_bound | 8 | 0.750 | 566.0 |
| distractor_robustness | core | native_grep | 4 | n/a | n/a |
| distractor_robustness | core | native_index | 4 | n/a | n/a |
| distractor_robustness | core | none | 4 | n/a | n/a |
| distractor_robustness | core | oracle | 4 | 1.000 | 296.5 |
| distractor_robustness | core | pull | 4 | 1.000 | 740.8 |
| distractor_robustness | core | push_breadcrumb | 4 | n/a | n/a |
| distractor_robustness | core | push_breadcrumb_pull | 4 | 1.000 | 1811.5 |
| distractor_robustness | core | push_pages_upper_bound | 4 | 1.000 | 711.5 |
| distractor_robustness | medium | native_grep | 4 | n/a | n/a |
| distractor_robustness | medium | native_index | 4 | n/a | n/a |
| distractor_robustness | medium | none | 4 | n/a | n/a |
| distractor_robustness | medium | oracle | 4 | 1.000 | 296.5 |
| distractor_robustness | medium | pull | 4 | 1.000 | 1158.2 |
| distractor_robustness | medium | push_breadcrumb | 4 | n/a | n/a |
| distractor_robustness | medium | push_breadcrumb_pull | 4 | 1.000 | 2584.2 |
| distractor_robustness | medium | push_pages_upper_bound | 4 | 0.900 | 601.0 |
| follow_through | core | native_grep | 11 | n/a | n/a |
| follow_through | core | native_index | 11 | n/a | n/a |
| follow_through | core | none | 12 | n/a | n/a |
| follow_through | core | oracle | 12 | 1.000 | 261.3 |
| follow_through | core | pull | 12 | 1.000 | 2576.7 |
| follow_through | core | push_breadcrumb | 12 | n/a | n/a |
| follow_through | core | push_breadcrumb_pull | 11 | 1.000 | 3409.4 |
| follow_through | core | push_pages_upper_bound | 12 | 1.000 | 733.0 |
| follow_through | medium | native_grep | 5 | n/a | n/a |
| follow_through | medium | native_index | 12 | n/a | n/a |
| follow_through | medium | none | 12 | n/a | n/a |
| follow_through | medium | oracle | 12 | 1.000 | 261.3 |
| follow_through | medium | pull | 11 | 1.000 | 3597.5 |
| follow_through | medium | push_breadcrumb | 12 | n/a | n/a |
| follow_through | medium | push_breadcrumb_pull | 12 | 1.000 | 4323.0 |
| follow_through | medium | push_pages_upper_bound | 12 | 0.983 | 734.4 |
| multi_hop | core | native_grep | 5 | n/a | n/a |
| multi_hop | core | native_index | 6 | n/a | n/a |
| multi_hop | core | none | 6 | n/a | n/a |
| multi_hop | core | oracle | 6 | 1.000 | 339.3 |
| multi_hop | core | pull | 4 | 1.000 | 2654.2 |
| multi_hop | core | push_breadcrumb | 6 | n/a | n/a |
| multi_hop | core | push_breadcrumb_pull | 5 | 1.000 | 4857.4 |
| multi_hop | core | push_pages_upper_bound | 6 | 1.000 | 702.3 |
| multi_hop | medium | native_grep | 3 | n/a | n/a |
| multi_hop | medium | native_index | 6 | n/a | n/a |
| multi_hop | medium | none | 6 | n/a | n/a |
| multi_hop | medium | oracle | 6 | 1.000 | 339.3 |
| multi_hop | medium | pull | 3 | 1.000 | 2963.7 |
| multi_hop | medium | push_breadcrumb | 6 | n/a | n/a |
| multi_hop | medium | push_breadcrumb_pull | 6 | 1.000 | 4816.8 |
| multi_hop | medium | push_pages_upper_bound | 6 | 1.000 | 688.0 |
| negative_knowledge | core | native_grep | 2 | n/a | n/a |
| negative_knowledge | core | native_index | 2 | n/a | n/a |
| negative_knowledge | core | none | 2 | n/a | n/a |
| negative_knowledge | core | oracle | 2 | 1.000 | 130.0 |
| negative_knowledge | core | pull | 2 | 1.000 | 1140.0 |
| negative_knowledge | core | push_breadcrumb | 2 | n/a | n/a |
| negative_knowledge | core | push_breadcrumb_pull | 2 | 1.000 | 2150.0 |
| negative_knowledge | core | push_pages_upper_bound | 2 | 1.000 | 702.0 |
| redundancy | core | native_grep | 8 | n/a | n/a |
| redundancy | core | native_index | 8 | n/a | n/a |
| redundancy | core | none | 8 | n/a | n/a |
| redundancy | core | oracle | 8 | 1.000 | 330.8 |
| redundancy | core | pull | 8 | 1.000 | 993.0 |
| redundancy | core | push_breadcrumb | 8 | n/a | n/a |
| redundancy | core | push_breadcrumb_pull | 8 | 0.975 | 1470.7 |
| redundancy | core | push_pages_upper_bound | 8 | 1.000 | 737.8 |
| redundancy | medium | native_grep | 8 | n/a | n/a |
| redundancy | medium | native_index | 8 | n/a | n/a |
| redundancy | medium | none | 8 | n/a | n/a |
| redundancy | medium | oracle | 8 | 1.000 | 330.8 |
| redundancy | medium | pull | 8 | 1.000 | 888.2 |
| redundancy | medium | push_breadcrumb | 8 | n/a | n/a |
| redundancy | medium | push_breadcrumb_pull | 8 | 0.963 | 1328.1 |
| redundancy | medium | push_pages_upper_bound | 8 | 0.925 | 704.1 |
| single_hop | core | native_grep | 16 | n/a | n/a |
| single_hop | core | native_index | 16 | n/a | n/a |
| single_hop | core | none | 16 | n/a | n/a |
| single_hop | core | oracle | 16 | 1.000 | 338.8 |
| single_hop | core | pull | 16 | 1.000 | 1203.4 |
| single_hop | core | push_breadcrumb | 16 | n/a | n/a |
| single_hop | core | push_breadcrumb_pull | 16 | 1.000 | 1727.5 |
| single_hop | core | push_pages_upper_bound | 16 | 0.975 | 652.9 |
| single_hop | medium | native_grep | 16 | n/a | n/a |
| single_hop | medium | native_index | 16 | n/a | n/a |
| single_hop | medium | none | 16 | n/a | n/a |
| single_hop | medium | oracle | 16 | 1.000 | 338.8 |
| single_hop | medium | pull | 16 | 1.000 | 1171.7 |
| single_hop | medium | push_breadcrumb | 16 | n/a | n/a |
| single_hop | medium | push_breadcrumb_pull | 16 | 0.994 | 2105.0 |
| single_hop | medium | push_pages_upper_bound | 16 | 0.912 | 661.9 |
| temporal | core | native_grep | 12 | n/a | n/a |
| temporal | core | native_index | 12 | n/a | n/a |
| temporal | core | none | 12 | n/a | n/a |
| temporal | core | oracle | 12 | 1.000 | 134.3 |
| temporal | core | pull | 12 | 0.983 | 637.3 |
| temporal | core | push_breadcrumb | 12 | n/a | n/a |
| temporal | core | push_breadcrumb_pull | 12 | 1.000 | 1024.8 |
| temporal | core | push_pages_upper_bound | 12 | 1.000 | 642.3 |
| temporal | medium | native_grep | 11 | n/a | n/a |
| temporal | medium | native_index | 12 | n/a | n/a |
| temporal | medium | none | 12 | n/a | n/a |
| temporal | medium | oracle | 12 | 1.000 | 134.3 |
| temporal | medium | pull | 12 | 1.000 | 835.8 |
| temporal | medium | push_breadcrumb | 12 | n/a | n/a |
| temporal | medium | push_breadcrumb_pull | 12 | 1.000 | 1247.0 |
| temporal | medium | push_pages_upper_bound | 12 | 1.000 | 669.7 |

## Utilization (free portion only)

`uid_citation_rate` = fraction of delivered uids whose literal uid string appears in the answer text. `distinctive_ngram_overlap` = `|ngrams(delivered) ∩ ngrams(answer)| / |ngrams(delivered)|` over 4-word shingles — denominator is the delivered text's own shingle count. Both are coarse, zero-judgment proxies for "did the answer draw on what was delivered" — the judged per-claim support check is explicitly out of scope for this report.

| probe_class | corpus_scale | arm | n | uid_citation_rate | distinctive_ngram_overlap |
| --- | --- | --- | --- | --- | --- |
| abstention | core | native_grep | 3 | n/a | n/a |
| abstention | core | native_index | 6 | n/a | n/a |
| abstention | core | none | 6 | n/a | n/a |
| abstention | core | oracle | 6 | n/a | n/a |
| abstention | core | pull | 4 | 0.000 | 0.001 |
| abstention | core | push_breadcrumb | 6 | n/a | 0.058 |
| abstention | core | push_breadcrumb_pull | 6 | 0.000 | 0.001 |
| abstention | core | push_pages_upper_bound | 6 | 0.000 | 0.000 |
| abstention | medium | native_grep | 4 | n/a | n/a |
| abstention | medium | native_index | 6 | n/a | n/a |
| abstention | medium | none | 6 | n/a | n/a |
| abstention | medium | oracle | 6 | n/a | n/a |
| abstention | medium | pull | 4 | 0.000 | 0.013 |
| abstention | medium | push_breadcrumb | 6 | n/a | 0.049 |
| abstention | medium | push_breadcrumb_pull | 5 | 0.020 | 0.010 |
| abstention | medium | push_pages_upper_bound | 6 | 0.067 | 0.032 |
| aggregation | core | native_grep | 5 | n/a | n/a |
| aggregation | core | native_index | 6 | n/a | n/a |
| aggregation | core | none | 6 | n/a | n/a |
| aggregation | core | oracle | 6 | 0.000 | 0.131 |
| aggregation | core | pull | 6 | 0.000 | 0.053 |
| aggregation | core | push_breadcrumb | 6 | n/a | 0.050 |
| aggregation | core | push_breadcrumb_pull | 6 | 0.000 | 0.062 |
| aggregation | core | push_pages_upper_bound | 6 | 0.000 | 0.086 |
| aggregation | medium | native_grep | 2 | n/a | n/a |
| aggregation | medium | native_index | 6 | n/a | n/a |
| aggregation | medium | none | 6 | n/a | n/a |
| aggregation | medium | oracle | 6 | 0.000 | 0.094 |
| aggregation | medium | pull | 5 | 0.000 | 0.040 |
| aggregation | medium | push_breadcrumb | 6 | n/a | 0.071 |
| aggregation | medium | push_breadcrumb_pull | 6 | 0.000 | 0.037 |
| aggregation | medium | push_pages_upper_bound | 6 | 0.033 | 0.103 |
| contradiction | core | native_grep | 6 | n/a | n/a |
| contradiction | core | native_index | 4 | n/a | n/a |
| contradiction | core | none | 6 | n/a | n/a |
| contradiction | core | oracle | 6 | 0.000 | 0.097 |
| contradiction | core | pull | 6 | 0.000 | 0.028 |
| contradiction | core | push_breadcrumb | 6 | n/a | 0.025 |
| contradiction | core | push_breadcrumb_pull | 6 | 0.017 | 0.024 |
| contradiction | core | push_pages_upper_bound | 6 | 0.000 | 0.037 |
| contradiction | medium | native_grep | 6 | n/a | n/a |
| contradiction | medium | native_index | 6 | n/a | n/a |
| contradiction | medium | none | 6 | n/a | n/a |
| contradiction | medium | oracle | 6 | 0.000 | 0.088 |
| contradiction | medium | pull | 6 | 0.000 | 0.022 |
| contradiction | medium | push_breadcrumb | 6 | n/a | 0.042 |
| contradiction | medium | push_breadcrumb_pull | 6 | 0.000 | 0.017 |
| contradiction | medium | push_pages_upper_bound | 6 | 0.167 | 0.061 |
| disambiguation | core | native_grep | 8 | n/a | n/a |
| disambiguation | core | native_index | 8 | n/a | n/a |
| disambiguation | core | none | 8 | n/a | n/a |
| disambiguation | core | oracle | 8 | 0.000 | 0.129 |
| disambiguation | core | pull | 8 | 0.050 | 0.058 |
| disambiguation | core | push_breadcrumb | 8 | n/a | 0.021 |
| disambiguation | core | push_breadcrumb_pull | 8 | 0.000 | 0.038 |
| disambiguation | core | push_pages_upper_bound | 8 | 0.250 | 0.087 |
| disambiguation | medium | native_grep | 8 | n/a | n/a |
| disambiguation | medium | native_index | 8 | n/a | n/a |
| disambiguation | medium | none | 8 | n/a | n/a |
| disambiguation | medium | oracle | 8 | 0.000 | 0.127 |
| disambiguation | medium | pull | 8 | 0.000 | 0.052 |
| disambiguation | medium | push_breadcrumb | 8 | n/a | 0.009 |
| disambiguation | medium | push_breadcrumb_pull | 8 | 0.087 | 0.034 |
| disambiguation | medium | push_pages_upper_bound | 8 | 0.250 | 0.080 |
| distractor_robustness | core | native_grep | 4 | n/a | n/a |
| distractor_robustness | core | native_index | 4 | n/a | n/a |
| distractor_robustness | core | none | 4 | n/a | n/a |
| distractor_robustness | core | oracle | 4 | 0.000 | 0.131 |
| distractor_robustness | core | pull | 4 | 0.000 | 0.087 |
| distractor_robustness | core | push_breadcrumb | 4 | n/a | 0.095 |
| distractor_robustness | core | push_breadcrumb_pull | 4 | 0.000 | 0.019 |
| distractor_robustness | core | push_pages_upper_bound | 4 | 0.000 | 0.069 |
| distractor_robustness | medium | native_grep | 4 | n/a | n/a |
| distractor_robustness | medium | native_index | 4 | n/a | n/a |
| distractor_robustness | medium | none | 4 | n/a | n/a |
| distractor_robustness | medium | oracle | 4 | 0.000 | 0.119 |
| distractor_robustness | medium | pull | 4 | 0.000 | 0.039 |
| distractor_robustness | medium | push_breadcrumb | 4 | n/a | 0.042 |
| distractor_robustness | medium | push_breadcrumb_pull | 4 | 0.000 | 0.027 |
| distractor_robustness | medium | push_pages_upper_bound | 4 | 0.100 | 0.050 |
| follow_through | core | native_grep | 11 | n/a | n/a |
| follow_through | core | native_index | 11 | n/a | n/a |
| follow_through | core | none | 12 | n/a | n/a |
| follow_through | core | oracle | 12 | 0.000 | 0.213 |
| follow_through | core | pull | 12 | 0.000 | 0.032 |
| follow_through | core | push_breadcrumb | 12 | n/a | 0.026 |
| follow_through | core | push_breadcrumb_pull | 11 | 0.000 | 0.019 |
| follow_through | core | push_pages_upper_bound | 12 | 0.000 | 0.030 |
| follow_through | medium | native_grep | 5 | n/a | n/a |
| follow_through | medium | native_index | 12 | n/a | n/a |
| follow_through | medium | none | 12 | n/a | n/a |
| follow_through | medium | oracle | 12 | 0.000 | 0.212 |
| follow_through | medium | pull | 11 | 0.000 | 0.026 |
| follow_through | medium | push_breadcrumb | 12 | n/a | 0.041 |
| follow_through | medium | push_breadcrumb_pull | 12 | 0.000 | 0.019 |
| follow_through | medium | push_pages_upper_bound | 12 | 0.017 | 0.042 |
| multi_hop | core | native_grep | 5 | n/a | n/a |
| multi_hop | core | native_index | 6 | n/a | n/a |
| multi_hop | core | none | 6 | n/a | n/a |
| multi_hop | core | oracle | 6 | 0.000 | 0.082 |
| multi_hop | core | pull | 4 | 0.000 | 0.023 |
| multi_hop | core | push_breadcrumb | 6 | n/a | 0.012 |
| multi_hop | core | push_breadcrumb_pull | 5 | 0.000 | 0.012 |
| multi_hop | core | push_pages_upper_bound | 6 | 0.000 | 0.026 |
| multi_hop | medium | native_grep | 3 | n/a | n/a |
| multi_hop | medium | native_index | 6 | n/a | n/a |
| multi_hop | medium | none | 6 | n/a | n/a |
| multi_hop | medium | oracle | 6 | 0.000 | 0.088 |
| multi_hop | medium | pull | 3 | 0.000 | 0.019 |
| multi_hop | medium | push_breadcrumb | 6 | n/a | 0.061 |
| multi_hop | medium | push_breadcrumb_pull | 6 | 0.000 | 0.016 |
| multi_hop | medium | push_pages_upper_bound | 6 | 0.000 | 0.026 |
| negative_knowledge | core | native_grep | 2 | n/a | n/a |
| negative_knowledge | core | native_index | 2 | n/a | n/a |
| negative_knowledge | core | none | 2 | n/a | n/a |
| negative_knowledge | core | oracle | 2 | 0.000 | 0.093 |
| negative_knowledge | core | pull | 2 | 0.000 | 0.057 |
| negative_knowledge | core | push_breadcrumb | 2 | n/a | 0.041 |
| negative_knowledge | core | push_breadcrumb_pull | 2 | 0.000 | 0.032 |
| negative_knowledge | core | push_pages_upper_bound | 2 | 0.000 | 0.067 |
| redundancy | core | native_grep | 8 | n/a | n/a |
| redundancy | core | native_index | 8 | n/a | n/a |
| redundancy | core | none | 8 | n/a | n/a |
| redundancy | core | oracle | 8 | 0.000 | 0.077 |
| redundancy | core | pull | 8 | 0.000 | 0.032 |
| redundancy | core | push_breadcrumb | 8 | n/a | 0.013 |
| redundancy | core | push_breadcrumb_pull | 8 | 0.025 | 0.029 |
| redundancy | core | push_pages_upper_bound | 8 | 0.000 | 0.031 |
| redundancy | medium | native_grep | 8 | n/a | n/a |
| redundancy | medium | native_index | 8 | n/a | n/a |
| redundancy | medium | none | 8 | n/a | n/a |
| redundancy | medium | oracle | 8 | 0.000 | 0.058 |
| redundancy | medium | pull | 8 | 0.000 | 0.027 |
| redundancy | medium | push_breadcrumb | 8 | n/a | 0.026 |
| redundancy | medium | push_breadcrumb_pull | 8 | 0.038 | 0.033 |
| redundancy | medium | push_pages_upper_bound | 8 | 0.075 | 0.031 |
| single_hop | core | native_grep | 16 | n/a | n/a |
| single_hop | core | native_index | 16 | n/a | n/a |
| single_hop | core | none | 16 | n/a | n/a |
| single_hop | core | oracle | 16 | 0.000 | 0.173 |
| single_hop | core | pull | 16 | 0.000 | 0.075 |
| single_hop | core | push_breadcrumb | 16 | n/a | 0.012 |
| single_hop | core | push_breadcrumb_pull | 16 | 0.000 | 0.048 |
| single_hop | core | push_pages_upper_bound | 16 | 0.025 | 0.069 |
| single_hop | medium | native_grep | 16 | n/a | n/a |
| single_hop | medium | native_index | 16 | n/a | n/a |
| single_hop | medium | none | 16 | n/a | n/a |
| single_hop | medium | oracle | 16 | 0.000 | 0.176 |
| single_hop | medium | pull | 16 | 0.000 | 0.063 |
| single_hop | medium | push_breadcrumb | 16 | n/a | 0.028 |
| single_hop | medium | push_breadcrumb_pull | 16 | 0.006 | 0.042 |
| single_hop | medium | push_pages_upper_bound | 16 | 0.088 | 0.065 |
| temporal | core | native_grep | 12 | n/a | n/a |
| temporal | core | native_index | 12 | n/a | n/a |
| temporal | core | none | 12 | n/a | n/a |
| temporal | core | oracle | 12 | 0.000 | 0.077 |
| temporal | core | pull | 12 | 0.017 | 0.029 |
| temporal | core | push_breadcrumb | 12 | n/a | 0.023 |
| temporal | core | push_breadcrumb_pull | 12 | 0.000 | 0.020 |
| temporal | core | push_pages_upper_bound | 12 | 0.000 | 0.036 |
| temporal | medium | native_grep | 11 | n/a | n/a |
| temporal | medium | native_index | 12 | n/a | n/a |
| temporal | medium | none | 12 | n/a | n/a |
| temporal | medium | oracle | 12 | 0.000 | 0.085 |
| temporal | medium | pull | 12 | 0.000 | 0.030 |
| temporal | medium | push_breadcrumb | 12 | n/a | 0.046 |
| temporal | medium | push_breadcrumb_pull | 12 | 0.000 | 0.021 |
| temporal | medium | push_pages_upper_bound | 12 | 0.000 | 0.042 |

## Correctness (answer ground truth, issue athenaeum#1573)

`correctness_rate` grades the ANSWER, not retrieval: normalized substring match against each probe's planted `answer_tokens` (non-abstention), or the declining-language rule for abstention probes — see `grade_correctness`. No LLM judge. This is what makes NONE (floor) and ORACLE (ceiling) readable as numbers for the first time — every other dimension above describes retrieval or cost, never whether the final answer was actually right. `n/a` means no probe in that group carries ground truth tokens to grade against. Those tokens are the corpus pages' own internal reference tags, so every arm's system prompt carries one identical instruction (issue athenaeum#1753) to end the answer with `[ref: TAG]` for each page relied on, or `[ref: none]` for none — correctness therefore reads as “did the arm reach the right page and say so”. Rows recorded before that contract landed carry no tags and grade at or near 0 for every arm, including ORACLE.

| probe_class | corpus_scale | arm | n | correctness_rate |
| --- | --- | --- | --- | --- |
| abstention | core | native_grep | 3 | 0.667 |
| abstention | core | native_index | 6 | 0.833 |
| abstention | core | none | 6 | 1.000 |
| abstention | core | oracle | 6 | 1.000 |
| abstention | core | pull | 4 | 1.000 |
| abstention | core | push_breadcrumb | 6 | 1.000 |
| abstention | core | push_breadcrumb_pull | 6 | 0.833 |
| abstention | core | push_pages_upper_bound | 6 | 1.000 |
| abstention | medium | native_grep | 4 | 1.000 |
| abstention | medium | native_index | 6 | 0.833 |
| abstention | medium | none | 6 | 1.000 |
| abstention | medium | oracle | 6 | 1.000 |
| abstention | medium | pull | 4 | 1.000 |
| abstention | medium | push_breadcrumb | 6 | 0.500 |
| abstention | medium | push_breadcrumb_pull | 5 | 1.000 |
| abstention | medium | push_pages_upper_bound | 6 | 1.000 |
| aggregation | core | native_grep | 5 | 1.000 |
| aggregation | core | native_index | 6 | 0.000 |
| aggregation | core | none | 6 | 0.000 |
| aggregation | core | oracle | 6 | 0.667 |
| aggregation | core | pull | 6 | 0.167 |
| aggregation | core | push_breadcrumb | 6 | 0.000 |
| aggregation | core | push_breadcrumb_pull | 6 | 0.333 |
| aggregation | core | push_pages_upper_bound | 6 | 0.000 |
| aggregation | medium | native_grep | 2 | 1.000 |
| aggregation | medium | native_index | 6 | 0.333 |
| aggregation | medium | none | 6 | 0.000 |
| aggregation | medium | oracle | 6 | 0.500 |
| aggregation | medium | pull | 5 | 0.000 |
| aggregation | medium | push_breadcrumb | 6 | 0.000 |
| aggregation | medium | push_breadcrumb_pull | 6 | 0.167 |
| aggregation | medium | push_pages_upper_bound | 6 | 0.000 |
| contradiction | core | native_grep | 6 | 0.667 |
| contradiction | core | native_index | 4 | 1.000 |
| contradiction | core | none | 6 | 0.000 |
| contradiction | core | oracle | 6 | 1.000 |
| contradiction | core | pull | 6 | 1.000 |
| contradiction | core | push_breadcrumb | 6 | 0.000 |
| contradiction | core | push_breadcrumb_pull | 6 | 1.000 |
| contradiction | core | push_pages_upper_bound | 6 | 0.333 |
| contradiction | medium | native_grep | 6 | 0.833 |
| contradiction | medium | native_index | 6 | 0.833 |
| contradiction | medium | none | 6 | 0.000 |
| contradiction | medium | oracle | 6 | 1.000 |
| contradiction | medium | pull | 6 | 1.000 |
| contradiction | medium | push_breadcrumb | 6 | 0.000 |
| contradiction | medium | push_breadcrumb_pull | 6 | 1.000 |
| contradiction | medium | push_pages_upper_bound | 6 | 0.333 |
| disambiguation | core | native_grep | 8 | 0.625 |
| disambiguation | core | native_index | 8 | 0.625 |
| disambiguation | core | none | 8 | 0.000 |
| disambiguation | core | oracle | 8 | 1.000 |
| disambiguation | core | pull | 8 | 1.000 |
| disambiguation | core | push_breadcrumb | 8 | 0.000 |
| disambiguation | core | push_breadcrumb_pull | 8 | 1.000 |
| disambiguation | core | push_pages_upper_bound | 8 | 1.000 |
| disambiguation | medium | native_grep | 8 | 0.625 |
| disambiguation | medium | native_index | 8 | 0.250 |
| disambiguation | medium | none | 8 | 0.000 |
| disambiguation | medium | oracle | 8 | 1.000 |
| disambiguation | medium | pull | 8 | 1.000 |
| disambiguation | medium | push_breadcrumb | 8 | 0.000 |
| disambiguation | medium | push_breadcrumb_pull | 8 | 1.000 |
| disambiguation | medium | push_pages_upper_bound | 8 | 1.000 |
| distractor_robustness | core | native_grep | 4 | 0.250 |
| distractor_robustness | core | native_index | 4 | 0.000 |
| distractor_robustness | core | none | 4 | 0.000 |
| distractor_robustness | core | oracle | 4 | 1.000 |
| distractor_robustness | core | pull | 4 | 0.750 |
| distractor_robustness | core | push_breadcrumb | 4 | 0.000 |
| distractor_robustness | core | push_breadcrumb_pull | 4 | 1.000 |
| distractor_robustness | core | push_pages_upper_bound | 4 | 1.000 |
| distractor_robustness | medium | native_grep | 4 | 0.500 |
| distractor_robustness | medium | native_index | 4 | 0.250 |
| distractor_robustness | medium | none | 4 | 0.000 |
| distractor_robustness | medium | oracle | 4 | 1.000 |
| distractor_robustness | medium | pull | 4 | 0.500 |
| distractor_robustness | medium | push_breadcrumb | 4 | 0.000 |
| distractor_robustness | medium | push_breadcrumb_pull | 4 | 1.000 |
| distractor_robustness | medium | push_pages_upper_bound | 4 | 0.500 |
| follow_through | core | native_grep | 11 | 0.818 |
| follow_through | core | native_index | 11 | 0.909 |
| follow_through | core | none | 12 | 0.000 |
| follow_through | core | oracle | 12 | 1.000 |
| follow_through | core | pull | 12 | 1.000 |
| follow_through | core | push_breadcrumb | 12 | 0.000 |
| follow_through | core | push_breadcrumb_pull | 11 | 0.909 |
| follow_through | core | push_pages_upper_bound | 12 | 0.000 |
| follow_through | medium | native_grep | 5 | 1.000 |
| follow_through | medium | native_index | 12 | 1.000 |
| follow_through | medium | none | 12 | 0.000 |
| follow_through | medium | oracle | 12 | 1.000 |
| follow_through | medium | pull | 11 | 1.000 |
| follow_through | medium | push_breadcrumb | 12 | 0.000 |
| follow_through | medium | push_breadcrumb_pull | 12 | 0.917 |
| follow_through | medium | push_pages_upper_bound | 12 | 0.000 |
| multi_hop | core | native_grep | 5 | 1.000 |
| multi_hop | core | native_index | 6 | 0.667 |
| multi_hop | core | none | 6 | 0.000 |
| multi_hop | core | oracle | 6 | 0.833 |
| multi_hop | core | pull | 4 | 1.000 |
| multi_hop | core | push_breadcrumb | 6 | 0.000 |
| multi_hop | core | push_breadcrumb_pull | 5 | 0.800 |
| multi_hop | core | push_pages_upper_bound | 6 | 0.000 |
| multi_hop | medium | native_grep | 3 | 0.667 |
| multi_hop | medium | native_index | 6 | 0.500 |
| multi_hop | medium | none | 6 | 0.000 |
| multi_hop | medium | oracle | 6 | 0.833 |
| multi_hop | medium | pull | 3 | 0.667 |
| multi_hop | medium | push_breadcrumb | 6 | 0.000 |
| multi_hop | medium | push_breadcrumb_pull | 6 | 0.500 |
| multi_hop | medium | push_pages_upper_bound | 6 | 0.000 |
| negative_knowledge | core | native_grep | 2 | 1.000 |
| negative_knowledge | core | native_index | 2 | 0.500 |
| negative_knowledge | core | none | 2 | 0.000 |
| negative_knowledge | core | oracle | 2 | 1.000 |
| negative_knowledge | core | pull | 2 | 1.000 |
| negative_knowledge | core | push_breadcrumb | 2 | 0.000 |
| negative_knowledge | core | push_breadcrumb_pull | 2 | 1.000 |
| negative_knowledge | core | push_pages_upper_bound | 2 | 1.000 |
| redundancy | core | native_grep | 8 | 0.250 |
| redundancy | core | native_index | 8 | 0.125 |
| redundancy | core | none | 8 | 0.000 |
| redundancy | core | oracle | 8 | 0.500 |
| redundancy | core | pull | 8 | 0.875 |
| redundancy | core | push_breadcrumb | 8 | 0.000 |
| redundancy | core | push_breadcrumb_pull | 8 | 0.750 |
| redundancy | core | push_pages_upper_bound | 8 | 0.750 |
| redundancy | medium | native_grep | 8 | 0.000 |
| redundancy | medium | native_index | 8 | 0.125 |
| redundancy | medium | none | 8 | 0.000 |
| redundancy | medium | oracle | 8 | 0.625 |
| redundancy | medium | pull | 8 | 0.750 |
| redundancy | medium | push_breadcrumb | 8 | 0.000 |
| redundancy | medium | push_breadcrumb_pull | 8 | 0.875 |
| redundancy | medium | push_pages_upper_bound | 8 | 0.875 |
| single_hop | core | native_grep | 16 | 0.938 |
| single_hop | core | native_index | 16 | 0.812 |
| single_hop | core | none | 16 | 0.000 |
| single_hop | core | oracle | 16 | 0.688 |
| single_hop | core | pull | 16 | 0.875 |
| single_hop | core | push_breadcrumb | 16 | 0.000 |
| single_hop | core | push_breadcrumb_pull | 16 | 0.812 |
| single_hop | core | push_pages_upper_bound | 16 | 0.375 |
| single_hop | medium | native_grep | 16 | 0.875 |
| single_hop | medium | native_index | 16 | 0.875 |
| single_hop | medium | none | 16 | 0.000 |
| single_hop | medium | oracle | 16 | 0.750 |
| single_hop | medium | pull | 16 | 0.938 |
| single_hop | medium | push_breadcrumb | 16 | 0.000 |
| single_hop | medium | push_breadcrumb_pull | 16 | 1.000 |
| single_hop | medium | push_pages_upper_bound | 16 | 0.375 |
| temporal | core | native_grep | 12 | 1.000 |
| temporal | core | native_index | 12 | 0.583 |
| temporal | core | none | 12 | 0.000 |
| temporal | core | oracle | 12 | 1.000 |
| temporal | core | pull | 12 | 0.917 |
| temporal | core | push_breadcrumb | 12 | 0.000 |
| temporal | core | push_breadcrumb_pull | 12 | 1.000 |
| temporal | core | push_pages_upper_bound | 12 | 1.000 |
| temporal | medium | native_grep | 11 | 0.727 |
| temporal | medium | native_index | 12 | 0.667 |
| temporal | medium | none | 12 | 0.000 |
| temporal | medium | oracle | 12 | 1.000 |
| temporal | medium | pull | 12 | 1.000 |
| temporal | medium | push_breadcrumb | 12 | 0.000 |
| temporal | medium | push_breadcrumb_pull | 12 | 1.000 |
| temporal | medium | push_pages_upper_bound | 12 | 1.000 |

## Harm (forbidden-token) rate (issue athenaeum#1772, report_only)

`harm_free_rate` grades whether the answer avoided every one of a probe's planted `forbidden_tokens` -- see `grade_harm`. Same normalizer and substring instrument as `correctness_rate`, no LLM judge. `report_only` (issue athenaeum#1791 §2.1): this mechanism feeds no §7 condition and `compute_verdicts` is unchanged by it. `n/a` means no probe in that group carries `forbidden_tokens` to grade against -- true for every probe class shipped so far, so this section reads `n/a` throughout the current corpus.

| probe_class | corpus_scale | arm | n | harm_free_rate |
| --- | --- | --- | --- | --- |
| abstention | core | native_grep | 3 | n/a |
| abstention | core | native_index | 6 | n/a |
| abstention | core | none | 6 | n/a |
| abstention | core | oracle | 6 | n/a |
| abstention | core | pull | 4 | n/a |
| abstention | core | push_breadcrumb | 6 | n/a |
| abstention | core | push_breadcrumb_pull | 6 | n/a |
| abstention | core | push_pages_upper_bound | 6 | n/a |
| abstention | medium | native_grep | 4 | n/a |
| abstention | medium | native_index | 6 | n/a |
| abstention | medium | none | 6 | n/a |
| abstention | medium | oracle | 6 | n/a |
| abstention | medium | pull | 4 | n/a |
| abstention | medium | push_breadcrumb | 6 | n/a |
| abstention | medium | push_breadcrumb_pull | 5 | n/a |
| abstention | medium | push_pages_upper_bound | 6 | n/a |
| aggregation | core | native_grep | 5 | n/a |
| aggregation | core | native_index | 6 | n/a |
| aggregation | core | none | 6 | n/a |
| aggregation | core | oracle | 6 | n/a |
| aggregation | core | pull | 6 | n/a |
| aggregation | core | push_breadcrumb | 6 | n/a |
| aggregation | core | push_breadcrumb_pull | 6 | n/a |
| aggregation | core | push_pages_upper_bound | 6 | n/a |
| aggregation | medium | native_grep | 2 | n/a |
| aggregation | medium | native_index | 6 | n/a |
| aggregation | medium | none | 6 | n/a |
| aggregation | medium | oracle | 6 | n/a |
| aggregation | medium | pull | 5 | n/a |
| aggregation | medium | push_breadcrumb | 6 | n/a |
| aggregation | medium | push_breadcrumb_pull | 6 | n/a |
| aggregation | medium | push_pages_upper_bound | 6 | n/a |
| contradiction | core | native_grep | 6 | 0.167 |
| contradiction | core | native_index | 4 | 0.750 |
| contradiction | core | none | 6 | 1.000 |
| contradiction | core | oracle | 6 | 1.000 |
| contradiction | core | pull | 6 | 0.667 |
| contradiction | core | push_breadcrumb | 6 | 1.000 |
| contradiction | core | push_breadcrumb_pull | 6 | 0.500 |
| contradiction | core | push_pages_upper_bound | 6 | 0.167 |
| contradiction | medium | native_grep | 6 | 0.167 |
| contradiction | medium | native_index | 6 | 0.333 |
| contradiction | medium | none | 6 | 1.000 |
| contradiction | medium | oracle | 6 | 1.000 |
| contradiction | medium | pull | 6 | 0.667 |
| contradiction | medium | push_breadcrumb | 6 | 1.000 |
| contradiction | medium | push_breadcrumb_pull | 6 | 0.333 |
| contradiction | medium | push_pages_upper_bound | 6 | 0.000 |
| disambiguation | core | native_grep | 8 | n/a |
| disambiguation | core | native_index | 8 | n/a |
| disambiguation | core | none | 8 | n/a |
| disambiguation | core | oracle | 8 | n/a |
| disambiguation | core | pull | 8 | n/a |
| disambiguation | core | push_breadcrumb | 8 | n/a |
| disambiguation | core | push_breadcrumb_pull | 8 | n/a |
| disambiguation | core | push_pages_upper_bound | 8 | n/a |
| disambiguation | medium | native_grep | 8 | n/a |
| disambiguation | medium | native_index | 8 | n/a |
| disambiguation | medium | none | 8 | n/a |
| disambiguation | medium | oracle | 8 | n/a |
| disambiguation | medium | pull | 8 | n/a |
| disambiguation | medium | push_breadcrumb | 8 | n/a |
| disambiguation | medium | push_breadcrumb_pull | 8 | n/a |
| disambiguation | medium | push_pages_upper_bound | 8 | n/a |
| distractor_robustness | core | native_grep | 4 | n/a |
| distractor_robustness | core | native_index | 4 | n/a |
| distractor_robustness | core | none | 4 | n/a |
| distractor_robustness | core | oracle | 4 | n/a |
| distractor_robustness | core | pull | 4 | n/a |
| distractor_robustness | core | push_breadcrumb | 4 | n/a |
| distractor_robustness | core | push_breadcrumb_pull | 4 | n/a |
| distractor_robustness | core | push_pages_upper_bound | 4 | n/a |
| distractor_robustness | medium | native_grep | 4 | n/a |
| distractor_robustness | medium | native_index | 4 | n/a |
| distractor_robustness | medium | none | 4 | n/a |
| distractor_robustness | medium | oracle | 4 | n/a |
| distractor_robustness | medium | pull | 4 | n/a |
| distractor_robustness | medium | push_breadcrumb | 4 | n/a |
| distractor_robustness | medium | push_breadcrumb_pull | 4 | n/a |
| distractor_robustness | medium | push_pages_upper_bound | 4 | n/a |
| follow_through | core | native_grep | 11 | n/a |
| follow_through | core | native_index | 11 | n/a |
| follow_through | core | none | 12 | n/a |
| follow_through | core | oracle | 12 | n/a |
| follow_through | core | pull | 12 | n/a |
| follow_through | core | push_breadcrumb | 12 | n/a |
| follow_through | core | push_breadcrumb_pull | 11 | n/a |
| follow_through | core | push_pages_upper_bound | 12 | n/a |
| follow_through | medium | native_grep | 5 | n/a |
| follow_through | medium | native_index | 12 | n/a |
| follow_through | medium | none | 12 | n/a |
| follow_through | medium | oracle | 12 | n/a |
| follow_through | medium | pull | 11 | n/a |
| follow_through | medium | push_breadcrumb | 12 | n/a |
| follow_through | medium | push_breadcrumb_pull | 12 | n/a |
| follow_through | medium | push_pages_upper_bound | 12 | n/a |
| multi_hop | core | native_grep | 5 | n/a |
| multi_hop | core | native_index | 6 | n/a |
| multi_hop | core | none | 6 | n/a |
| multi_hop | core | oracle | 6 | n/a |
| multi_hop | core | pull | 4 | n/a |
| multi_hop | core | push_breadcrumb | 6 | n/a |
| multi_hop | core | push_breadcrumb_pull | 5 | n/a |
| multi_hop | core | push_pages_upper_bound | 6 | n/a |
| multi_hop | medium | native_grep | 3 | n/a |
| multi_hop | medium | native_index | 6 | n/a |
| multi_hop | medium | none | 6 | n/a |
| multi_hop | medium | oracle | 6 | n/a |
| multi_hop | medium | pull | 3 | n/a |
| multi_hop | medium | push_breadcrumb | 6 | n/a |
| multi_hop | medium | push_breadcrumb_pull | 6 | n/a |
| multi_hop | medium | push_pages_upper_bound | 6 | n/a |
| negative_knowledge | core | native_grep | 2 | 1.000 |
| negative_knowledge | core | native_index | 2 | 0.000 |
| negative_knowledge | core | none | 2 | 1.000 |
| negative_knowledge | core | oracle | 2 | 1.000 |
| negative_knowledge | core | pull | 2 | 0.000 |
| negative_knowledge | core | push_breadcrumb | 2 | 1.000 |
| negative_knowledge | core | push_breadcrumb_pull | 2 | 0.000 |
| negative_knowledge | core | push_pages_upper_bound | 2 | 0.500 |
| redundancy | core | native_grep | 8 | n/a |
| redundancy | core | native_index | 8 | n/a |
| redundancy | core | none | 8 | n/a |
| redundancy | core | oracle | 8 | n/a |
| redundancy | core | pull | 8 | n/a |
| redundancy | core | push_breadcrumb | 8 | n/a |
| redundancy | core | push_breadcrumb_pull | 8 | n/a |
| redundancy | core | push_pages_upper_bound | 8 | n/a |
| redundancy | medium | native_grep | 8 | n/a |
| redundancy | medium | native_index | 8 | n/a |
| redundancy | medium | none | 8 | n/a |
| redundancy | medium | oracle | 8 | n/a |
| redundancy | medium | pull | 8 | n/a |
| redundancy | medium | push_breadcrumb | 8 | n/a |
| redundancy | medium | push_breadcrumb_pull | 8 | n/a |
| redundancy | medium | push_pages_upper_bound | 8 | n/a |
| single_hop | core | native_grep | 16 | n/a |
| single_hop | core | native_index | 16 | n/a |
| single_hop | core | none | 16 | n/a |
| single_hop | core | oracle | 16 | n/a |
| single_hop | core | pull | 16 | n/a |
| single_hop | core | push_breadcrumb | 16 | n/a |
| single_hop | core | push_breadcrumb_pull | 16 | n/a |
| single_hop | core | push_pages_upper_bound | 16 | n/a |
| single_hop | medium | native_grep | 16 | n/a |
| single_hop | medium | native_index | 16 | n/a |
| single_hop | medium | none | 16 | n/a |
| single_hop | medium | oracle | 16 | n/a |
| single_hop | medium | pull | 16 | n/a |
| single_hop | medium | push_breadcrumb | 16 | n/a |
| single_hop | medium | push_breadcrumb_pull | 16 | n/a |
| single_hop | medium | push_pages_upper_bound | 16 | n/a |
| temporal | core | native_grep | 12 | n/a |
| temporal | core | native_index | 12 | n/a |
| temporal | core | none | 12 | n/a |
| temporal | core | oracle | 12 | n/a |
| temporal | core | pull | 12 | n/a |
| temporal | core | push_breadcrumb | 12 | n/a |
| temporal | core | push_breadcrumb_pull | 12 | n/a |
| temporal | core | push_pages_upper_bound | 12 | n/a |
| temporal | medium | native_grep | 11 | n/a |
| temporal | medium | native_index | 12 | n/a |
| temporal | medium | none | 12 | n/a |
| temporal | medium | oracle | 12 | n/a |
| temporal | medium | pull | 12 | n/a |
| temporal | medium | push_breadcrumb | 12 | n/a |
| temporal | medium | push_breadcrumb_pull | 12 | n/a |
| temporal | medium | push_pages_upper_bound | 12 | n/a |

## Coverage (fraction of planted tokens) (issue athenaeum#1773, report_only)

`coverage_rate` is the mean fraction of a probe's `answer_tokens` present in the answer -- see `grade_coverage`. Same normalizer and substring instrument as `correctness_rate`, but a ratio rather than an all-or-nothing match, so a many-correct-answer probe (the `aggregation` class) can grade partial credit instead of a forced zero. `report_only` (issue athenaeum#1791 §2.1): this mechanism feeds no §7 condition and `compute_verdicts` is unchanged by it. `n/a` means no probe in that group carries `answer_tokens` to grade against. For a single-`answer_tokens` probe, `coverage_rate` and `correctness_rate` are the same number by construction.

| probe_class | corpus_scale | arm | n | coverage_rate |
| --- | --- | --- | --- | --- |
| abstention | core | native_grep | 3 | n/a |
| abstention | core | native_index | 6 | n/a |
| abstention | core | none | 6 | n/a |
| abstention | core | oracle | 6 | n/a |
| abstention | core | pull | 4 | n/a |
| abstention | core | push_breadcrumb | 6 | n/a |
| abstention | core | push_breadcrumb_pull | 6 | n/a |
| abstention | core | push_pages_upper_bound | 6 | n/a |
| abstention | medium | native_grep | 4 | n/a |
| abstention | medium | native_index | 6 | n/a |
| abstention | medium | none | 6 | n/a |
| abstention | medium | oracle | 6 | n/a |
| abstention | medium | pull | 4 | n/a |
| abstention | medium | push_breadcrumb | 6 | n/a |
| abstention | medium | push_breadcrumb_pull | 5 | n/a |
| abstention | medium | push_pages_upper_bound | 6 | n/a |
| aggregation | core | native_grep | 5 | 1.000 |
| aggregation | core | native_index | 6 | 0.028 |
| aggregation | core | none | 6 | 0.000 |
| aggregation | core | oracle | 6 | 1.000 |
| aggregation | core | pull | 6 | 0.706 |
| aggregation | core | push_breadcrumb | 6 | 0.000 |
| aggregation | core | push_breadcrumb_pull | 6 | 0.782 |
| aggregation | core | push_pages_upper_bound | 6 | 0.357 |
| aggregation | medium | native_grep | 2 | 1.000 |
| aggregation | medium | native_index | 6 | 0.381 |
| aggregation | medium | none | 6 | 0.000 |
| aggregation | medium | oracle | 6 | 1.000 |
| aggregation | medium | pull | 5 | 0.710 |
| aggregation | medium | push_breadcrumb | 6 | 0.000 |
| aggregation | medium | push_breadcrumb_pull | 6 | 0.810 |
| aggregation | medium | push_pages_upper_bound | 6 | 0.167 |
| contradiction | core | native_grep | 6 | 0.667 |
| contradiction | core | native_index | 4 | 1.000 |
| contradiction | core | none | 6 | 0.000 |
| contradiction | core | oracle | 6 | 0.333 |
| contradiction | core | pull | 6 | 1.000 |
| contradiction | core | push_breadcrumb | 6 | 0.000 |
| contradiction | core | push_breadcrumb_pull | 6 | 1.000 |
| contradiction | core | push_pages_upper_bound | 6 | 0.333 |
| contradiction | medium | native_grep | 6 | 0.833 |
| contradiction | medium | native_index | 6 | 0.833 |
| contradiction | medium | none | 6 | 0.000 |
| contradiction | medium | oracle | 6 | 0.333 |
| contradiction | medium | pull | 6 | 1.000 |
| contradiction | medium | push_breadcrumb | 6 | 0.000 |
| contradiction | medium | push_breadcrumb_pull | 6 | 1.000 |
| contradiction | medium | push_pages_upper_bound | 6 | 0.333 |
| disambiguation | core | native_grep | 8 | 0.625 |
| disambiguation | core | native_index | 8 | 0.625 |
| disambiguation | core | none | 8 | 0.000 |
| disambiguation | core | oracle | 8 | 1.000 |
| disambiguation | core | pull | 8 | 0.375 |
| disambiguation | core | push_breadcrumb | 8 | 0.000 |
| disambiguation | core | push_breadcrumb_pull | 8 | 0.250 |
| disambiguation | core | push_pages_upper_bound | 8 | 0.250 |
| disambiguation | medium | native_grep | 8 | 0.625 |
| disambiguation | medium | native_index | 8 | 0.250 |
| disambiguation | medium | none | 8 | 0.000 |
| disambiguation | medium | oracle | 8 | 1.000 |
| disambiguation | medium | pull | 8 | 0.500 |
| disambiguation | medium | push_breadcrumb | 8 | 0.000 |
| disambiguation | medium | push_breadcrumb_pull | 8 | 0.250 |
| disambiguation | medium | push_pages_upper_bound | 8 | 0.375 |
| distractor_robustness | core | native_grep | 4 | 0.500 |
| distractor_robustness | core | native_index | 4 | 0.000 |
| distractor_robustness | core | none | 4 | 0.000 |
| distractor_robustness | core | oracle | 4 | 1.000 |
| distractor_robustness | core | pull | 4 | 0.750 |
| distractor_robustness | core | push_breadcrumb | 4 | 0.000 |
| distractor_robustness | core | push_breadcrumb_pull | 4 | 1.000 |
| distractor_robustness | core | push_pages_upper_bound | 4 | 1.000 |
| distractor_robustness | medium | native_grep | 4 | 0.500 |
| distractor_robustness | medium | native_index | 4 | 0.000 |
| distractor_robustness | medium | none | 4 | 0.000 |
| distractor_robustness | medium | oracle | 4 | 1.000 |
| distractor_robustness | medium | pull | 4 | 0.500 |
| distractor_robustness | medium | push_breadcrumb | 4 | 0.000 |
| distractor_robustness | medium | push_breadcrumb_pull | 4 | 1.000 |
| distractor_robustness | medium | push_pages_upper_bound | 4 | 0.500 |
| follow_through | core | native_grep | 11 | 0.909 |
| follow_through | core | native_index | 11 | 1.000 |
| follow_through | core | none | 12 | 0.000 |
| follow_through | core | oracle | 12 | 1.000 |
| follow_through | core | pull | 12 | 1.000 |
| follow_through | core | push_breadcrumb | 12 | 0.000 |
| follow_through | core | push_breadcrumb_pull | 11 | 0.955 |
| follow_through | core | push_pages_upper_bound | 12 | 0.500 |
| follow_through | medium | native_grep | 5 | 1.000 |
| follow_through | medium | native_index | 12 | 1.000 |
| follow_through | medium | none | 12 | 0.000 |
| follow_through | medium | oracle | 12 | 1.000 |
| follow_through | medium | pull | 11 | 1.000 |
| follow_through | medium | push_breadcrumb | 12 | 0.000 |
| follow_through | medium | push_breadcrumb_pull | 12 | 1.000 |
| follow_through | medium | push_pages_upper_bound | 12 | 0.500 |
| multi_hop | core | native_grep | 5 | 1.000 |
| multi_hop | core | native_index | 6 | 0.667 |
| multi_hop | core | none | 6 | 0.000 |
| multi_hop | core | oracle | 6 | 1.000 |
| multi_hop | core | pull | 4 | 1.000 |
| multi_hop | core | push_breadcrumb | 6 | 0.000 |
| multi_hop | core | push_breadcrumb_pull | 5 | 0.800 |
| multi_hop | core | push_pages_upper_bound | 6 | 0.000 |
| multi_hop | medium | native_grep | 3 | 0.667 |
| multi_hop | medium | native_index | 6 | 0.667 |
| multi_hop | medium | none | 6 | 0.000 |
| multi_hop | medium | oracle | 6 | 1.000 |
| multi_hop | medium | pull | 3 | 1.000 |
| multi_hop | medium | push_breadcrumb | 6 | 0.000 |
| multi_hop | medium | push_breadcrumb_pull | 6 | 0.667 |
| multi_hop | medium | push_pages_upper_bound | 6 | 0.000 |
| negative_knowledge | core | native_grep | 2 | 1.000 |
| negative_knowledge | core | native_index | 2 | 1.000 |
| negative_knowledge | core | none | 2 | 0.000 |
| negative_knowledge | core | oracle | 2 | 1.000 |
| negative_knowledge | core | pull | 2 | 1.000 |
| negative_knowledge | core | push_breadcrumb | 2 | 0.000 |
| negative_knowledge | core | push_breadcrumb_pull | 2 | 1.000 |
| negative_knowledge | core | push_pages_upper_bound | 2 | 1.000 |
| redundancy | core | native_grep | 8 | 0.625 |
| redundancy | core | native_index | 8 | 0.625 |
| redundancy | core | none | 8 | 0.000 |
| redundancy | core | oracle | 8 | 0.938 |
| redundancy | core | pull | 8 | 0.625 |
| redundancy | core | push_breadcrumb | 8 | 0.000 |
| redundancy | core | push_breadcrumb_pull | 8 | 0.375 |
| redundancy | core | push_pages_upper_bound | 8 | 0.375 |
| redundancy | medium | native_grep | 8 | 0.625 |
| redundancy | medium | native_index | 8 | 0.625 |
| redundancy | medium | none | 8 | 0.000 |
| redundancy | medium | oracle | 8 | 0.938 |
| redundancy | medium | pull | 8 | 0.500 |
| redundancy | medium | push_breadcrumb | 8 | 0.000 |
| redundancy | medium | push_breadcrumb_pull | 8 | 0.438 |
| redundancy | medium | push_pages_upper_bound | 8 | 0.500 |
| single_hop | core | native_grep | 16 | 1.000 |
| single_hop | core | native_index | 16 | 0.938 |
| single_hop | core | none | 16 | 0.000 |
| single_hop | core | oracle | 16 | 1.000 |
| single_hop | core | pull | 16 | 1.000 |
| single_hop | core | push_breadcrumb | 16 | 0.000 |
| single_hop | core | push_breadcrumb_pull | 16 | 1.000 |
| single_hop | core | push_pages_upper_bound | 16 | 0.500 |
| single_hop | medium | native_grep | 16 | 0.938 |
| single_hop | medium | native_index | 16 | 0.938 |
| single_hop | medium | none | 16 | 0.000 |
| single_hop | medium | oracle | 16 | 1.000 |
| single_hop | medium | pull | 16 | 1.000 |
| single_hop | medium | push_breadcrumb | 16 | 0.000 |
| single_hop | medium | push_breadcrumb_pull | 16 | 1.000 |
| single_hop | medium | push_pages_upper_bound | 16 | 0.500 |
| temporal | core | native_grep | 12 | 1.000 |
| temporal | core | native_index | 12 | 0.583 |
| temporal | core | none | 12 | 0.000 |
| temporal | core | oracle | 12 | 1.000 |
| temporal | core | pull | 12 | 1.000 |
| temporal | core | push_breadcrumb | 12 | 0.000 |
| temporal | core | push_breadcrumb_pull | 12 | 1.000 |
| temporal | core | push_pages_upper_bound | 12 | 1.000 |
| temporal | medium | native_grep | 11 | 0.818 |
| temporal | medium | native_index | 12 | 0.667 |
| temporal | medium | none | 12 | 0.000 |
| temporal | medium | oracle | 12 | 1.000 |
| temporal | medium | pull | 12 | 0.917 |
| temporal | medium | push_breadcrumb | 12 | 0.000 |
| temporal | medium | push_breadcrumb_pull | 12 | 0.917 |
| temporal | medium | push_pages_upper_bound | 12 | 1.000 |

## Marker resolution (issue athenaeum#1730, report_only)

`marker_resolution_rate` is the share of answers whose CITED `[^src-N]` footnote markers all resolve to a footnote definition on a page that plants one of the probe's `answer_tokens` -- see `grade_marker_resolution`. It asks whether an answer that quotes a citation quoted a real one, now that compiled pages carry inline per-claim markers and `recall` resolves them on the hit. `report_only`: this mechanism feeds no §7 condition and `compute_verdicts` is unchanged by it. `n/a` means no answer in that group cited a marker, or no token-bearing expected page defines one -- both are 'nothing to grade', never a graded zero.

| probe_class | corpus_scale | arm | n | marker_resolution_rate |
| --- | --- | --- | --- | --- |
| abstention | core | native_grep | 3 | n/a |
| abstention | core | native_index | 6 | n/a |
| abstention | core | none | 6 | n/a |
| abstention | core | oracle | 6 | n/a |
| abstention | core | pull | 4 | n/a |
| abstention | core | push_breadcrumb | 6 | n/a |
| abstention | core | push_breadcrumb_pull | 6 | n/a |
| abstention | core | push_pages_upper_bound | 6 | n/a |
| abstention | medium | native_grep | 4 | n/a |
| abstention | medium | native_index | 6 | n/a |
| abstention | medium | none | 6 | n/a |
| abstention | medium | oracle | 6 | n/a |
| abstention | medium | pull | 4 | n/a |
| abstention | medium | push_breadcrumb | 6 | n/a |
| abstention | medium | push_breadcrumb_pull | 5 | n/a |
| abstention | medium | push_pages_upper_bound | 6 | n/a |
| aggregation | core | native_grep | 5 | n/a |
| aggregation | core | native_index | 6 | n/a |
| aggregation | core | none | 6 | n/a |
| aggregation | core | oracle | 6 | n/a |
| aggregation | core | pull | 6 | n/a |
| aggregation | core | push_breadcrumb | 6 | n/a |
| aggregation | core | push_breadcrumb_pull | 6 | n/a |
| aggregation | core | push_pages_upper_bound | 6 | n/a |
| aggregation | medium | native_grep | 2 | n/a |
| aggregation | medium | native_index | 6 | n/a |
| aggregation | medium | none | 6 | n/a |
| aggregation | medium | oracle | 6 | n/a |
| aggregation | medium | pull | 5 | n/a |
| aggregation | medium | push_breadcrumb | 6 | n/a |
| aggregation | medium | push_breadcrumb_pull | 6 | n/a |
| aggregation | medium | push_pages_upper_bound | 6 | n/a |
| contradiction | core | native_grep | 6 | n/a |
| contradiction | core | native_index | 4 | n/a |
| contradiction | core | none | 6 | n/a |
| contradiction | core | oracle | 6 | n/a |
| contradiction | core | pull | 6 | n/a |
| contradiction | core | push_breadcrumb | 6 | n/a |
| contradiction | core | push_breadcrumb_pull | 6 | n/a |
| contradiction | core | push_pages_upper_bound | 6 | n/a |
| contradiction | medium | native_grep | 6 | n/a |
| contradiction | medium | native_index | 6 | n/a |
| contradiction | medium | none | 6 | n/a |
| contradiction | medium | oracle | 6 | n/a |
| contradiction | medium | pull | 6 | n/a |
| contradiction | medium | push_breadcrumb | 6 | n/a |
| contradiction | medium | push_breadcrumb_pull | 6 | n/a |
| contradiction | medium | push_pages_upper_bound | 6 | n/a |
| disambiguation | core | native_grep | 8 | n/a |
| disambiguation | core | native_index | 8 | n/a |
| disambiguation | core | none | 8 | n/a |
| disambiguation | core | oracle | 8 | n/a |
| disambiguation | core | pull | 8 | n/a |
| disambiguation | core | push_breadcrumb | 8 | n/a |
| disambiguation | core | push_breadcrumb_pull | 8 | n/a |
| disambiguation | core | push_pages_upper_bound | 8 | n/a |
| disambiguation | medium | native_grep | 8 | n/a |
| disambiguation | medium | native_index | 8 | n/a |
| disambiguation | medium | none | 8 | n/a |
| disambiguation | medium | oracle | 8 | n/a |
| disambiguation | medium | pull | 8 | n/a |
| disambiguation | medium | push_breadcrumb | 8 | n/a |
| disambiguation | medium | push_breadcrumb_pull | 8 | n/a |
| disambiguation | medium | push_pages_upper_bound | 8 | n/a |
| distractor_robustness | core | native_grep | 4 | n/a |
| distractor_robustness | core | native_index | 4 | n/a |
| distractor_robustness | core | none | 4 | n/a |
| distractor_robustness | core | oracle | 4 | n/a |
| distractor_robustness | core | pull | 4 | n/a |
| distractor_robustness | core | push_breadcrumb | 4 | n/a |
| distractor_robustness | core | push_breadcrumb_pull | 4 | n/a |
| distractor_robustness | core | push_pages_upper_bound | 4 | n/a |
| distractor_robustness | medium | native_grep | 4 | n/a |
| distractor_robustness | medium | native_index | 4 | n/a |
| distractor_robustness | medium | none | 4 | n/a |
| distractor_robustness | medium | oracle | 4 | n/a |
| distractor_robustness | medium | pull | 4 | n/a |
| distractor_robustness | medium | push_breadcrumb | 4 | n/a |
| distractor_robustness | medium | push_breadcrumb_pull | 4 | n/a |
| distractor_robustness | medium | push_pages_upper_bound | 4 | n/a |
| follow_through | core | native_grep | 11 | n/a |
| follow_through | core | native_index | 11 | n/a |
| follow_through | core | none | 12 | n/a |
| follow_through | core | oracle | 12 | n/a |
| follow_through | core | pull | 12 | n/a |
| follow_through | core | push_breadcrumb | 12 | n/a |
| follow_through | core | push_breadcrumb_pull | 11 | n/a |
| follow_through | core | push_pages_upper_bound | 12 | n/a |
| follow_through | medium | native_grep | 5 | n/a |
| follow_through | medium | native_index | 12 | n/a |
| follow_through | medium | none | 12 | n/a |
| follow_through | medium | oracle | 12 | n/a |
| follow_through | medium | pull | 11 | n/a |
| follow_through | medium | push_breadcrumb | 12 | n/a |
| follow_through | medium | push_breadcrumb_pull | 12 | n/a |
| follow_through | medium | push_pages_upper_bound | 12 | n/a |
| multi_hop | core | native_grep | 5 | n/a |
| multi_hop | core | native_index | 6 | n/a |
| multi_hop | core | none | 6 | n/a |
| multi_hop | core | oracle | 6 | n/a |
| multi_hop | core | pull | 4 | n/a |
| multi_hop | core | push_breadcrumb | 6 | n/a |
| multi_hop | core | push_breadcrumb_pull | 5 | n/a |
| multi_hop | core | push_pages_upper_bound | 6 | n/a |
| multi_hop | medium | native_grep | 3 | n/a |
| multi_hop | medium | native_index | 6 | n/a |
| multi_hop | medium | none | 6 | n/a |
| multi_hop | medium | oracle | 6 | n/a |
| multi_hop | medium | pull | 3 | n/a |
| multi_hop | medium | push_breadcrumb | 6 | n/a |
| multi_hop | medium | push_breadcrumb_pull | 6 | n/a |
| multi_hop | medium | push_pages_upper_bound | 6 | n/a |
| negative_knowledge | core | native_grep | 2 | n/a |
| negative_knowledge | core | native_index | 2 | n/a |
| negative_knowledge | core | none | 2 | n/a |
| negative_knowledge | core | oracle | 2 | n/a |
| negative_knowledge | core | pull | 2 | n/a |
| negative_knowledge | core | push_breadcrumb | 2 | n/a |
| negative_knowledge | core | push_breadcrumb_pull | 2 | n/a |
| negative_knowledge | core | push_pages_upper_bound | 2 | n/a |
| redundancy | core | native_grep | 8 | n/a |
| redundancy | core | native_index | 8 | n/a |
| redundancy | core | none | 8 | n/a |
| redundancy | core | oracle | 8 | n/a |
| redundancy | core | pull | 8 | n/a |
| redundancy | core | push_breadcrumb | 8 | n/a |
| redundancy | core | push_breadcrumb_pull | 8 | n/a |
| redundancy | core | push_pages_upper_bound | 8 | n/a |
| redundancy | medium | native_grep | 8 | n/a |
| redundancy | medium | native_index | 8 | n/a |
| redundancy | medium | none | 8 | n/a |
| redundancy | medium | oracle | 8 | n/a |
| redundancy | medium | pull | 8 | n/a |
| redundancy | medium | push_breadcrumb | 8 | n/a |
| redundancy | medium | push_breadcrumb_pull | 8 | n/a |
| redundancy | medium | push_pages_upper_bound | 8 | n/a |
| single_hop | core | native_grep | 16 | n/a |
| single_hop | core | native_index | 16 | n/a |
| single_hop | core | none | 16 | n/a |
| single_hop | core | oracle | 16 | n/a |
| single_hop | core | pull | 16 | n/a |
| single_hop | core | push_breadcrumb | 16 | n/a |
| single_hop | core | push_breadcrumb_pull | 16 | n/a |
| single_hop | core | push_pages_upper_bound | 16 | n/a |
| single_hop | medium | native_grep | 16 | n/a |
| single_hop | medium | native_index | 16 | n/a |
| single_hop | medium | none | 16 | n/a |
| single_hop | medium | oracle | 16 | n/a |
| single_hop | medium | pull | 16 | n/a |
| single_hop | medium | push_breadcrumb | 16 | n/a |
| single_hop | medium | push_breadcrumb_pull | 16 | n/a |
| single_hop | medium | push_pages_upper_bound | 16 | n/a |
| temporal | core | native_grep | 12 | n/a |
| temporal | core | native_index | 12 | n/a |
| temporal | core | none | 12 | n/a |
| temporal | core | oracle | 12 | n/a |
| temporal | core | pull | 12 | n/a |
| temporal | core | push_breadcrumb | 12 | n/a |
| temporal | core | push_breadcrumb_pull | 12 | n/a |
| temporal | core | push_pages_upper_bound | 12 | n/a |
| temporal | medium | native_grep | 11 | n/a |
| temporal | medium | native_index | 12 | n/a |
| temporal | medium | none | 12 | n/a |
| temporal | medium | oracle | 12 | n/a |
| temporal | medium | pull | 12 | n/a |
| temporal | medium | push_breadcrumb | 12 | n/a |
| temporal | medium | push_breadcrumb_pull | 12 | n/a |
| temporal | medium | push_pages_upper_bound | 12 | n/a |

## Reference tag followed (issue athenaeum#1831, report_only)

`tag_followed_rate` is the share of answers that quoted the `[ref: TAG]` `REFERENCE_TAG_INSTRUCTION` asks for, for every one of the probe's planted tags -- see `tag_followed`. This is the athenaeum#1753 correctness rule, DEMOTED by the operator ruling on athenaeum#1791 comment 5732689494: it feeds no §7 condition and no `correctness_rate` above, which is now graded on `answer_markers` (the planted fact) plus delivered-page evidence, never this tag. `n/a` means no probe in that group carries `answer_tokens` to grade against (including every abstention group).

| probe_class | corpus_scale | arm | n | tag_followed_rate |
| --- | --- | --- | --- | --- |
| abstention | core | native_grep | 3 | n/a |
| abstention | core | native_index | 6 | n/a |
| abstention | core | none | 6 | n/a |
| abstention | core | oracle | 6 | n/a |
| abstention | core | pull | 4 | n/a |
| abstention | core | push_breadcrumb | 6 | n/a |
| abstention | core | push_breadcrumb_pull | 6 | n/a |
| abstention | core | push_pages_upper_bound | 6 | n/a |
| abstention | medium | native_grep | 4 | n/a |
| abstention | medium | native_index | 6 | n/a |
| abstention | medium | none | 6 | n/a |
| abstention | medium | oracle | 6 | n/a |
| abstention | medium | pull | 4 | n/a |
| abstention | medium | push_breadcrumb | 6 | n/a |
| abstention | medium | push_breadcrumb_pull | 5 | n/a |
| abstention | medium | push_pages_upper_bound | 6 | n/a |
| aggregation | core | native_grep | 5 | 1.000 |
| aggregation | core | native_index | 6 | 0.000 |
| aggregation | core | none | 6 | 0.000 |
| aggregation | core | oracle | 6 | 1.000 |
| aggregation | core | pull | 6 | 0.167 |
| aggregation | core | push_breadcrumb | 6 | 0.000 |
| aggregation | core | push_breadcrumb_pull | 6 | 0.333 |
| aggregation | core | push_pages_upper_bound | 6 | 0.000 |
| aggregation | medium | native_grep | 2 | 1.000 |
| aggregation | medium | native_index | 6 | 0.333 |
| aggregation | medium | none | 6 | 0.000 |
| aggregation | medium | oracle | 6 | 1.000 |
| aggregation | medium | pull | 5 | 0.200 |
| aggregation | medium | push_breadcrumb | 6 | 0.000 |
| aggregation | medium | push_breadcrumb_pull | 6 | 0.667 |
| aggregation | medium | push_pages_upper_bound | 6 | 0.000 |
| contradiction | core | native_grep | 6 | 0.667 |
| contradiction | core | native_index | 4 | 1.000 |
| contradiction | core | none | 6 | 0.000 |
| contradiction | core | oracle | 6 | 0.333 |
| contradiction | core | pull | 6 | 1.000 |
| contradiction | core | push_breadcrumb | 6 | 0.000 |
| contradiction | core | push_breadcrumb_pull | 6 | 1.000 |
| contradiction | core | push_pages_upper_bound | 6 | 0.333 |
| contradiction | medium | native_grep | 6 | 0.833 |
| contradiction | medium | native_index | 6 | 0.833 |
| contradiction | medium | none | 6 | 0.000 |
| contradiction | medium | oracle | 6 | 0.333 |
| contradiction | medium | pull | 6 | 1.000 |
| contradiction | medium | push_breadcrumb | 6 | 0.000 |
| contradiction | medium | push_breadcrumb_pull | 6 | 1.000 |
| contradiction | medium | push_pages_upper_bound | 6 | 0.333 |
| disambiguation | core | native_grep | 8 | 0.625 |
| disambiguation | core | native_index | 8 | 0.625 |
| disambiguation | core | none | 8 | 0.000 |
| disambiguation | core | oracle | 8 | 1.000 |
| disambiguation | core | pull | 8 | 0.375 |
| disambiguation | core | push_breadcrumb | 8 | 0.000 |
| disambiguation | core | push_breadcrumb_pull | 8 | 0.250 |
| disambiguation | core | push_pages_upper_bound | 8 | 0.250 |
| disambiguation | medium | native_grep | 8 | 0.625 |
| disambiguation | medium | native_index | 8 | 0.250 |
| disambiguation | medium | none | 8 | 0.000 |
| disambiguation | medium | oracle | 8 | 1.000 |
| disambiguation | medium | pull | 8 | 0.500 |
| disambiguation | medium | push_breadcrumb | 8 | 0.000 |
| disambiguation | medium | push_breadcrumb_pull | 8 | 0.250 |
| disambiguation | medium | push_pages_upper_bound | 8 | 0.375 |
| distractor_robustness | core | native_grep | 4 | 0.500 |
| distractor_robustness | core | native_index | 4 | 0.000 |
| distractor_robustness | core | none | 4 | 0.000 |
| distractor_robustness | core | oracle | 4 | 1.000 |
| distractor_robustness | core | pull | 4 | 0.750 |
| distractor_robustness | core | push_breadcrumb | 4 | 0.000 |
| distractor_robustness | core | push_breadcrumb_pull | 4 | 1.000 |
| distractor_robustness | core | push_pages_upper_bound | 4 | 1.000 |
| distractor_robustness | medium | native_grep | 4 | 0.500 |
| distractor_robustness | medium | native_index | 4 | 0.000 |
| distractor_robustness | medium | none | 4 | 0.000 |
| distractor_robustness | medium | oracle | 4 | 1.000 |
| distractor_robustness | medium | pull | 4 | 0.500 |
| distractor_robustness | medium | push_breadcrumb | 4 | 0.000 |
| distractor_robustness | medium | push_breadcrumb_pull | 4 | 1.000 |
| distractor_robustness | medium | push_pages_upper_bound | 4 | 0.500 |
| follow_through | core | native_grep | 11 | 0.818 |
| follow_through | core | native_index | 11 | 1.000 |
| follow_through | core | none | 12 | 0.000 |
| follow_through | core | oracle | 12 | 1.000 |
| follow_through | core | pull | 12 | 1.000 |
| follow_through | core | push_breadcrumb | 12 | 0.000 |
| follow_through | core | push_breadcrumb_pull | 11 | 0.909 |
| follow_through | core | push_pages_upper_bound | 12 | 0.000 |
| follow_through | medium | native_grep | 5 | 1.000 |
| follow_through | medium | native_index | 12 | 1.000 |
| follow_through | medium | none | 12 | 0.000 |
| follow_through | medium | oracle | 12 | 1.000 |
| follow_through | medium | pull | 11 | 1.000 |
| follow_through | medium | push_breadcrumb | 12 | 0.000 |
| follow_through | medium | push_breadcrumb_pull | 12 | 1.000 |
| follow_through | medium | push_pages_upper_bound | 12 | 0.000 |
| multi_hop | core | native_grep | 5 | 1.000 |
| multi_hop | core | native_index | 6 | 0.667 |
| multi_hop | core | none | 6 | 0.000 |
| multi_hop | core | oracle | 6 | 1.000 |
| multi_hop | core | pull | 4 | 1.000 |
| multi_hop | core | push_breadcrumb | 6 | 0.000 |
| multi_hop | core | push_breadcrumb_pull | 5 | 0.800 |
| multi_hop | core | push_pages_upper_bound | 6 | 0.000 |
| multi_hop | medium | native_grep | 3 | 0.667 |
| multi_hop | medium | native_index | 6 | 0.667 |
| multi_hop | medium | none | 6 | 0.000 |
| multi_hop | medium | oracle | 6 | 1.000 |
| multi_hop | medium | pull | 3 | 1.000 |
| multi_hop | medium | push_breadcrumb | 6 | 0.000 |
| multi_hop | medium | push_breadcrumb_pull | 6 | 0.667 |
| multi_hop | medium | push_pages_upper_bound | 6 | 0.000 |
| negative_knowledge | core | native_grep | 2 | 1.000 |
| negative_knowledge | core | native_index | 2 | 1.000 |
| negative_knowledge | core | none | 2 | 0.000 |
| negative_knowledge | core | oracle | 2 | 1.000 |
| negative_knowledge | core | pull | 2 | 1.000 |
| negative_knowledge | core | push_breadcrumb | 2 | 0.000 |
| negative_knowledge | core | push_breadcrumb_pull | 2 | 1.000 |
| negative_knowledge | core | push_pages_upper_bound | 2 | 1.000 |
| redundancy | core | native_grep | 8 | 0.250 |
| redundancy | core | native_index | 8 | 0.250 |
| redundancy | core | none | 8 | 0.000 |
| redundancy | core | oracle | 8 | 0.875 |
| redundancy | core | pull | 8 | 0.250 |
| redundancy | core | push_breadcrumb | 8 | 0.000 |
| redundancy | core | push_breadcrumb_pull | 8 | 0.000 |
| redundancy | core | push_pages_upper_bound | 8 | 0.000 |
| redundancy | medium | native_grep | 8 | 0.250 |
| redundancy | medium | native_index | 8 | 0.250 |
| redundancy | medium | none | 8 | 0.000 |
| redundancy | medium | oracle | 8 | 0.875 |
| redundancy | medium | pull | 8 | 0.250 |
| redundancy | medium | push_breadcrumb | 8 | 0.000 |
| redundancy | medium | push_breadcrumb_pull | 8 | 0.125 |
| redundancy | medium | push_pages_upper_bound | 8 | 0.250 |
| single_hop | core | native_grep | 16 | 1.000 |
| single_hop | core | native_index | 16 | 0.938 |
| single_hop | core | none | 16 | 0.000 |
| single_hop | core | oracle | 16 | 1.000 |
| single_hop | core | pull | 16 | 1.000 |
| single_hop | core | push_breadcrumb | 16 | 0.000 |
| single_hop | core | push_breadcrumb_pull | 16 | 1.000 |
| single_hop | core | push_pages_upper_bound | 16 | 0.500 |
| single_hop | medium | native_grep | 16 | 0.938 |
| single_hop | medium | native_index | 16 | 0.938 |
| single_hop | medium | none | 16 | 0.000 |
| single_hop | medium | oracle | 16 | 1.000 |
| single_hop | medium | pull | 16 | 1.000 |
| single_hop | medium | push_breadcrumb | 16 | 0.000 |
| single_hop | medium | push_breadcrumb_pull | 16 | 1.000 |
| single_hop | medium | push_pages_upper_bound | 16 | 0.500 |
| temporal | core | native_grep | 12 | 1.000 |
| temporal | core | native_index | 12 | 0.583 |
| temporal | core | none | 12 | 0.000 |
| temporal | core | oracle | 12 | 1.000 |
| temporal | core | pull | 12 | 1.000 |
| temporal | core | push_breadcrumb | 12 | 0.000 |
| temporal | core | push_breadcrumb_pull | 12 | 1.000 |
| temporal | core | push_pages_upper_bound | 12 | 1.000 |
| temporal | medium | native_grep | 11 | 0.818 |
| temporal | medium | native_index | 12 | 0.667 |
| temporal | medium | none | 12 | 0.000 |
| temporal | medium | oracle | 12 | 1.000 |
| temporal | medium | pull | 12 | 0.917 |
| temporal | medium | push_breadcrumb | 12 | 0.000 |
| temporal | medium | push_breadcrumb_pull | 12 | 0.917 |
| temporal | medium | push_pages_upper_bound | 12 | 1.000 |

## Marker miss with delivery (issue athenaeum#1842, report_only)

`marker_miss_with_delivery` is the COUNT of gradable cells in that group where every answer-bearing page WAS delivered to the arm (`correctness_rate`'s delivered-page-evidence clause held) and at least one of those pages still contributed no matching `answer_markers` value to the answer -- see `marker_miss_with_delivery`. It splits a `False` cell into its two causes: a delivery gap (counted here as 0) versus a model miss or a marker-matching gap (counted here as 1). `report_only`: this column feeds no §7 condition, no `compute_verdicts` input and no win/loss field, exactly as `tag_followed_rate` above does not. `n/a` means no row in that group is gradable at all (including every abstention group) -- never a counted zero.

| probe_class | corpus_scale | arm | n | marker_miss_with_delivery |
| --- | --- | --- | --- | --- |
| abstention | core | native_grep | 3 | n/a |
| abstention | core | native_index | 6 | n/a |
| abstention | core | none | 6 | n/a |
| abstention | core | oracle | 6 | n/a |
| abstention | core | pull | 4 | n/a |
| abstention | core | push_breadcrumb | 6 | n/a |
| abstention | core | push_breadcrumb_pull | 6 | n/a |
| abstention | core | push_pages_upper_bound | 6 | n/a |
| abstention | medium | native_grep | 4 | n/a |
| abstention | medium | native_index | 6 | n/a |
| abstention | medium | none | 6 | n/a |
| abstention | medium | oracle | 6 | n/a |
| abstention | medium | pull | 4 | n/a |
| abstention | medium | push_breadcrumb | 6 | n/a |
| abstention | medium | push_breadcrumb_pull | 5 | n/a |
| abstention | medium | push_pages_upper_bound | 6 | n/a |
| aggregation | core | native_grep | 5 | 0 |
| aggregation | core | native_index | 6 | 0 |
| aggregation | core | none | 6 | 0 |
| aggregation | core | oracle | 6 | 2 |
| aggregation | core | pull | 6 | 0 |
| aggregation | core | push_breadcrumb | 6 | 0 |
| aggregation | core | push_breadcrumb_pull | 6 | 2 |
| aggregation | core | push_pages_upper_bound | 6 | 0 |
| aggregation | medium | native_grep | 2 | 0 |
| aggregation | medium | native_index | 6 | 0 |
| aggregation | medium | none | 6 | 0 |
| aggregation | medium | oracle | 6 | 3 |
| aggregation | medium | pull | 5 | 1 |
| aggregation | medium | push_breadcrumb | 6 | 0 |
| aggregation | medium | push_breadcrumb_pull | 6 | 3 |
| aggregation | medium | push_pages_upper_bound | 6 | 0 |
| contradiction | core | native_grep | 6 | 0 |
| contradiction | core | native_index | 4 | 0 |
| contradiction | core | none | 6 | 0 |
| contradiction | core | oracle | 6 | 0 |
| contradiction | core | pull | 6 | 0 |
| contradiction | core | push_breadcrumb | 6 | 2 |
| contradiction | core | push_breadcrumb_pull | 6 | 0 |
| contradiction | core | push_pages_upper_bound | 6 | 0 |
| contradiction | medium | native_grep | 6 | 0 |
| contradiction | medium | native_index | 6 | 0 |
| contradiction | medium | none | 6 | 0 |
| contradiction | medium | oracle | 6 | 0 |
| contradiction | medium | pull | 6 | 0 |
| contradiction | medium | push_breadcrumb | 6 | 2 |
| contradiction | medium | push_breadcrumb_pull | 6 | 0 |
| contradiction | medium | push_pages_upper_bound | 6 | 0 |
| disambiguation | core | native_grep | 8 | 0 |
| disambiguation | core | native_index | 8 | 0 |
| disambiguation | core | none | 8 | 0 |
| disambiguation | core | oracle | 8 | 0 |
| disambiguation | core | pull | 8 | 0 |
| disambiguation | core | push_breadcrumb | 8 | 8 |
| disambiguation | core | push_breadcrumb_pull | 8 | 0 |
| disambiguation | core | push_pages_upper_bound | 8 | 0 |
| disambiguation | medium | native_grep | 8 | 0 |
| disambiguation | medium | native_index | 8 | 0 |
| disambiguation | medium | none | 8 | 0 |
| disambiguation | medium | oracle | 8 | 0 |
| disambiguation | medium | pull | 8 | 0 |
| disambiguation | medium | push_breadcrumb | 8 | 8 |
| disambiguation | medium | push_breadcrumb_pull | 8 | 0 |
| disambiguation | medium | push_pages_upper_bound | 8 | 0 |
| distractor_robustness | core | native_grep | 4 | 1 |
| distractor_robustness | core | native_index | 4 | 0 |
| distractor_robustness | core | none | 4 | 0 |
| distractor_robustness | core | oracle | 4 | 0 |
| distractor_robustness | core | pull | 4 | 0 |
| distractor_robustness | core | push_breadcrumb | 4 | 2 |
| distractor_robustness | core | push_breadcrumb_pull | 4 | 0 |
| distractor_robustness | core | push_pages_upper_bound | 4 | 0 |
| distractor_robustness | medium | native_grep | 4 | 0 |
| distractor_robustness | medium | native_index | 4 | 0 |
| distractor_robustness | medium | none | 4 | 0 |
| distractor_robustness | medium | oracle | 4 | 0 |
| distractor_robustness | medium | pull | 4 | 0 |
| distractor_robustness | medium | push_breadcrumb | 4 | 2 |
| distractor_robustness | medium | push_breadcrumb_pull | 4 | 0 |
| distractor_robustness | medium | push_pages_upper_bound | 4 | 0 |
| follow_through | core | native_grep | 11 | 0 |
| follow_through | core | native_index | 11 | 1 |
| follow_through | core | none | 12 | 0 |
| follow_through | core | oracle | 12 | 0 |
| follow_through | core | pull | 12 | 0 |
| follow_through | core | push_breadcrumb | 12 | 0 |
| follow_through | core | push_breadcrumb_pull | 11 | 0 |
| follow_through | core | push_pages_upper_bound | 12 | 0 |
| follow_through | medium | native_grep | 5 | 0 |
| follow_through | medium | native_index | 12 | 0 |
| follow_through | medium | none | 12 | 0 |
| follow_through | medium | oracle | 12 | 0 |
| follow_through | medium | pull | 11 | 0 |
| follow_through | medium | push_breadcrumb | 12 | 0 |
| follow_through | medium | push_breadcrumb_pull | 12 | 1 |
| follow_through | medium | push_pages_upper_bound | 12 | 0 |
| multi_hop | core | native_grep | 5 | 0 |
| multi_hop | core | native_index | 6 | 0 |
| multi_hop | core | none | 6 | 0 |
| multi_hop | core | oracle | 6 | 1 |
| multi_hop | core | pull | 4 | 0 |
| multi_hop | core | push_breadcrumb | 6 | 0 |
| multi_hop | core | push_breadcrumb_pull | 5 | 0 |
| multi_hop | core | push_pages_upper_bound | 6 | 0 |
| multi_hop | medium | native_grep | 3 | 0 |
| multi_hop | medium | native_index | 6 | 1 |
| multi_hop | medium | none | 6 | 0 |
| multi_hop | medium | oracle | 6 | 1 |
| multi_hop | medium | pull | 3 | 1 |
| multi_hop | medium | push_breadcrumb | 6 | 0 |
| multi_hop | medium | push_breadcrumb_pull | 6 | 1 |
| multi_hop | medium | push_pages_upper_bound | 6 | 0 |
| negative_knowledge | core | native_grep | 2 | 0 |
| negative_knowledge | core | native_index | 2 | 1 |
| negative_knowledge | core | none | 2 | 0 |
| negative_knowledge | core | oracle | 2 | 0 |
| negative_knowledge | core | pull | 2 | 0 |
| negative_knowledge | core | push_breadcrumb | 2 | 2 |
| negative_knowledge | core | push_breadcrumb_pull | 2 | 0 |
| negative_knowledge | core | push_pages_upper_bound | 2 | 0 |
| redundancy | core | native_grep | 8 | 1 |
| redundancy | core | native_index | 8 | 2 |
| redundancy | core | none | 8 | 0 |
| redundancy | core | oracle | 8 | 4 |
| redundancy | core | pull | 8 | 1 |
| redundancy | core | push_breadcrumb | 8 | 8 |
| redundancy | core | push_breadcrumb_pull | 8 | 2 |
| redundancy | core | push_pages_upper_bound | 8 | 2 |
| redundancy | medium | native_grep | 8 | 2 |
| redundancy | medium | native_index | 8 | 3 |
| redundancy | medium | none | 8 | 0 |
| redundancy | medium | oracle | 8 | 3 |
| redundancy | medium | pull | 8 | 2 |
| redundancy | medium | push_breadcrumb | 8 | 8 |
| redundancy | medium | push_breadcrumb_pull | 8 | 1 |
| redundancy | medium | push_pages_upper_bound | 8 | 1 |
| single_hop | core | native_grep | 16 | 1 |
| single_hop | core | native_index | 16 | 2 |
| single_hop | core | none | 16 | 0 |
| single_hop | core | oracle | 16 | 5 |
| single_hop | core | pull | 16 | 2 |
| single_hop | core | push_breadcrumb | 16 | 14 |
| single_hop | core | push_breadcrumb_pull | 16 | 3 |
| single_hop | core | push_pages_upper_bound | 16 | 10 |
| single_hop | medium | native_grep | 16 | 1 |
| single_hop | medium | native_index | 16 | 1 |
| single_hop | medium | none | 16 | 0 |
| single_hop | medium | oracle | 16 | 4 |
| single_hop | medium | pull | 16 | 1 |
| single_hop | medium | push_breadcrumb | 16 | 12 |
| single_hop | medium | push_breadcrumb_pull | 16 | 0 |
| single_hop | medium | push_pages_upper_bound | 16 | 10 |
| temporal | core | native_grep | 12 | 0 |
| temporal | core | native_index | 12 | 0 |
| temporal | core | none | 12 | 0 |
| temporal | core | oracle | 12 | 0 |
| temporal | core | pull | 12 | 1 |
| temporal | core | push_breadcrumb | 12 | 10 |
| temporal | core | push_breadcrumb_pull | 12 | 0 |
| temporal | core | push_pages_upper_bound | 12 | 0 |
| temporal | medium | native_grep | 11 | 1 |
| temporal | medium | native_index | 12 | 0 |
| temporal | medium | none | 12 | 0 |
| temporal | medium | oracle | 12 | 0 |
| temporal | medium | pull | 12 | 0 |
| temporal | medium | push_breadcrumb | 12 | 10 |
| temporal | medium | push_breadcrumb_pull | 12 | 0 |
| temporal | medium | push_pages_upper_bound | 12 | 0 |

## Follow-through hop delivered (issue athenaeum#1844, report_only)

`follow_hop_rate` is the share of `follow_through` cells in that group where every page named by `corpus.deep_hop_uids` -- the page an arm can only have landed on by FOLLOWING the body `[[wikilink]]`, never by a lexical match on the query -- was delivered to the arm, read from `_delivered_uids` (the grader's own dispatch). It separates the two causes a `False` `correctness_rate` conflates on this class: the arm never reached the second page (counted here as 0) versus it reached the page and the answer still did not carry the fact (counted here as 1, alongside a `marker_miss_with_delivery` of 1). The deep page is DERIVED from the link graph, never read off a fixed `expected_uids` position. `report_only`: this column feeds no §7 condition, no cutoff, no `compute_verdicts` input and no win/loss field, exactly as `marker_miss_with_delivery` above does not. `n/a` for every non-`follow_through` group -- no hop to have followed is a different fact from a rate of zero.

| probe_class | corpus_scale | arm | n | follow_hop_rate |
| --- | --- | --- | --- | --- |
| abstention | core | native_grep | 3 | n/a |
| abstention | core | native_index | 6 | n/a |
| abstention | core | none | 6 | n/a |
| abstention | core | oracle | 6 | n/a |
| abstention | core | pull | 4 | n/a |
| abstention | core | push_breadcrumb | 6 | n/a |
| abstention | core | push_breadcrumb_pull | 6 | n/a |
| abstention | core | push_pages_upper_bound | 6 | n/a |
| abstention | medium | native_grep | 4 | n/a |
| abstention | medium | native_index | 6 | n/a |
| abstention | medium | none | 6 | n/a |
| abstention | medium | oracle | 6 | n/a |
| abstention | medium | pull | 4 | n/a |
| abstention | medium | push_breadcrumb | 6 | n/a |
| abstention | medium | push_breadcrumb_pull | 5 | n/a |
| abstention | medium | push_pages_upper_bound | 6 | n/a |
| aggregation | core | native_grep | 5 | n/a |
| aggregation | core | native_index | 6 | n/a |
| aggregation | core | none | 6 | n/a |
| aggregation | core | oracle | 6 | n/a |
| aggregation | core | pull | 6 | n/a |
| aggregation | core | push_breadcrumb | 6 | n/a |
| aggregation | core | push_breadcrumb_pull | 6 | n/a |
| aggregation | core | push_pages_upper_bound | 6 | n/a |
| aggregation | medium | native_grep | 2 | n/a |
| aggregation | medium | native_index | 6 | n/a |
| aggregation | medium | none | 6 | n/a |
| aggregation | medium | oracle | 6 | n/a |
| aggregation | medium | pull | 5 | n/a |
| aggregation | medium | push_breadcrumb | 6 | n/a |
| aggregation | medium | push_breadcrumb_pull | 6 | n/a |
| aggregation | medium | push_pages_upper_bound | 6 | n/a |
| contradiction | core | native_grep | 6 | n/a |
| contradiction | core | native_index | 4 | n/a |
| contradiction | core | none | 6 | n/a |
| contradiction | core | oracle | 6 | n/a |
| contradiction | core | pull | 6 | n/a |
| contradiction | core | push_breadcrumb | 6 | n/a |
| contradiction | core | push_breadcrumb_pull | 6 | n/a |
| contradiction | core | push_pages_upper_bound | 6 | n/a |
| contradiction | medium | native_grep | 6 | n/a |
| contradiction | medium | native_index | 6 | n/a |
| contradiction | medium | none | 6 | n/a |
| contradiction | medium | oracle | 6 | n/a |
| contradiction | medium | pull | 6 | n/a |
| contradiction | medium | push_breadcrumb | 6 | n/a |
| contradiction | medium | push_breadcrumb_pull | 6 | n/a |
| contradiction | medium | push_pages_upper_bound | 6 | n/a |
| disambiguation | core | native_grep | 8 | n/a |
| disambiguation | core | native_index | 8 | n/a |
| disambiguation | core | none | 8 | n/a |
| disambiguation | core | oracle | 8 | n/a |
| disambiguation | core | pull | 8 | n/a |
| disambiguation | core | push_breadcrumb | 8 | n/a |
| disambiguation | core | push_breadcrumb_pull | 8 | n/a |
| disambiguation | core | push_pages_upper_bound | 8 | n/a |
| disambiguation | medium | native_grep | 8 | n/a |
| disambiguation | medium | native_index | 8 | n/a |
| disambiguation | medium | none | 8 | n/a |
| disambiguation | medium | oracle | 8 | n/a |
| disambiguation | medium | pull | 8 | n/a |
| disambiguation | medium | push_breadcrumb | 8 | n/a |
| disambiguation | medium | push_breadcrumb_pull | 8 | n/a |
| disambiguation | medium | push_pages_upper_bound | 8 | n/a |
| distractor_robustness | core | native_grep | 4 | n/a |
| distractor_robustness | core | native_index | 4 | n/a |
| distractor_robustness | core | none | 4 | n/a |
| distractor_robustness | core | oracle | 4 | n/a |
| distractor_robustness | core | pull | 4 | n/a |
| distractor_robustness | core | push_breadcrumb | 4 | n/a |
| distractor_robustness | core | push_breadcrumb_pull | 4 | n/a |
| distractor_robustness | core | push_pages_upper_bound | 4 | n/a |
| distractor_robustness | medium | native_grep | 4 | n/a |
| distractor_robustness | medium | native_index | 4 | n/a |
| distractor_robustness | medium | none | 4 | n/a |
| distractor_robustness | medium | oracle | 4 | n/a |
| distractor_robustness | medium | pull | 4 | n/a |
| distractor_robustness | medium | push_breadcrumb | 4 | n/a |
| distractor_robustness | medium | push_breadcrumb_pull | 4 | n/a |
| distractor_robustness | medium | push_pages_upper_bound | 4 | n/a |
| follow_through | core | native_grep | 11 | 0.818 |
| follow_through | core | native_index | 11 | 1.000 |
| follow_through | core | none | 12 | 0.000 |
| follow_through | core | oracle | 12 | 1.000 |
| follow_through | core | pull | 12 | 1.000 |
| follow_through | core | push_breadcrumb | 12 | 0.000 |
| follow_through | core | push_breadcrumb_pull | 11 | 0.909 |
| follow_through | core | push_pages_upper_bound | 12 | 0.000 |
| follow_through | medium | native_grep | 5 | 1.000 |
| follow_through | medium | native_index | 12 | 1.000 |
| follow_through | medium | none | 12 | 0.000 |
| follow_through | medium | oracle | 12 | 1.000 |
| follow_through | medium | pull | 11 | 1.000 |
| follow_through | medium | push_breadcrumb | 12 | 0.000 |
| follow_through | medium | push_breadcrumb_pull | 12 | 1.000 |
| follow_through | medium | push_pages_upper_bound | 12 | 0.000 |
| multi_hop | core | native_grep | 5 | n/a |
| multi_hop | core | native_index | 6 | n/a |
| multi_hop | core | none | 6 | n/a |
| multi_hop | core | oracle | 6 | n/a |
| multi_hop | core | pull | 4 | n/a |
| multi_hop | core | push_breadcrumb | 6 | n/a |
| multi_hop | core | push_breadcrumb_pull | 5 | n/a |
| multi_hop | core | push_pages_upper_bound | 6 | n/a |
| multi_hop | medium | native_grep | 3 | n/a |
| multi_hop | medium | native_index | 6 | n/a |
| multi_hop | medium | none | 6 | n/a |
| multi_hop | medium | oracle | 6 | n/a |
| multi_hop | medium | pull | 3 | n/a |
| multi_hop | medium | push_breadcrumb | 6 | n/a |
| multi_hop | medium | push_breadcrumb_pull | 6 | n/a |
| multi_hop | medium | push_pages_upper_bound | 6 | n/a |
| negative_knowledge | core | native_grep | 2 | n/a |
| negative_knowledge | core | native_index | 2 | n/a |
| negative_knowledge | core | none | 2 | n/a |
| negative_knowledge | core | oracle | 2 | n/a |
| negative_knowledge | core | pull | 2 | n/a |
| negative_knowledge | core | push_breadcrumb | 2 | n/a |
| negative_knowledge | core | push_breadcrumb_pull | 2 | n/a |
| negative_knowledge | core | push_pages_upper_bound | 2 | n/a |
| redundancy | core | native_grep | 8 | n/a |
| redundancy | core | native_index | 8 | n/a |
| redundancy | core | none | 8 | n/a |
| redundancy | core | oracle | 8 | n/a |
| redundancy | core | pull | 8 | n/a |
| redundancy | core | push_breadcrumb | 8 | n/a |
| redundancy | core | push_breadcrumb_pull | 8 | n/a |
| redundancy | core | push_pages_upper_bound | 8 | n/a |
| redundancy | medium | native_grep | 8 | n/a |
| redundancy | medium | native_index | 8 | n/a |
| redundancy | medium | none | 8 | n/a |
| redundancy | medium | oracle | 8 | n/a |
| redundancy | medium | pull | 8 | n/a |
| redundancy | medium | push_breadcrumb | 8 | n/a |
| redundancy | medium | push_breadcrumb_pull | 8 | n/a |
| redundancy | medium | push_pages_upper_bound | 8 | n/a |
| single_hop | core | native_grep | 16 | n/a |
| single_hop | core | native_index | 16 | n/a |
| single_hop | core | none | 16 | n/a |
| single_hop | core | oracle | 16 | n/a |
| single_hop | core | pull | 16 | n/a |
| single_hop | core | push_breadcrumb | 16 | n/a |
| single_hop | core | push_breadcrumb_pull | 16 | n/a |
| single_hop | core | push_pages_upper_bound | 16 | n/a |
| single_hop | medium | native_grep | 16 | n/a |
| single_hop | medium | native_index | 16 | n/a |
| single_hop | medium | none | 16 | n/a |
| single_hop | medium | oracle | 16 | n/a |
| single_hop | medium | pull | 16 | n/a |
| single_hop | medium | push_breadcrumb | 16 | n/a |
| single_hop | medium | push_breadcrumb_pull | 16 | n/a |
| single_hop | medium | push_pages_upper_bound | 16 | n/a |
| temporal | core | native_grep | 12 | n/a |
| temporal | core | native_index | 12 | n/a |
| temporal | core | none | 12 | n/a |
| temporal | core | oracle | 12 | n/a |
| temporal | core | pull | 12 | n/a |
| temporal | core | push_breadcrumb | 12 | n/a |
| temporal | core | push_breadcrumb_pull | 12 | n/a |
| temporal | core | push_pages_upper_bound | 12 | n/a |
| temporal | medium | native_grep | 11 | n/a |
| temporal | medium | native_index | 12 | n/a |
| temporal | medium | none | 12 | n/a |
| temporal | medium | oracle | 12 | n/a |
| temporal | medium | pull | 12 | n/a |
| temporal | medium | push_breadcrumb | 12 | n/a |
| temporal | medium | push_breadcrumb_pull | 12 | n/a |
| temporal | medium | push_pages_upper_bound | 12 | n/a |

### Weak probes (NONE arm already answers correctly)

Probes where the floor (no context at all) already grades correct — a signal the ground truth leaked into the model's own prior knowledge, or the token is otherwise guessable, not that any retrieval arm helped. Read alongside the ORACLE ceiling: a probe listed here needs a harder token or a different question, not a better arm.

_none observed in this run_

## Index coverage (NATIVE_INDEX only, athenaeum#1725)

`index_coverage = index_lines_loaded / pages` -- the fraction of the corpus the TRUNCATED index still names. `index_lines_loaded` is read back from what Claude Code actually loaded into the session (the `attachment` event in the session transcript), never recomputed from the 200-line/25KB cap: Claude Code performs the truncation, this report only observes its result. `n/a` means no NATIVE_INDEX row in that group carried a readable coverage figure (e.g. auto memory did not activate).

| probe_class | corpus_scale | n | index_coverage |
| --- | --- | --- | --- |
| abstention | core | 6 | 1.000 |
| abstention | medium | 6 | 0.200 |
| aggregation | core | 6 | 1.000 |
| aggregation | medium | 6 | 0.200 |
| contradiction | core | 4 | 1.000 |
| contradiction | medium | 6 | 0.200 |
| disambiguation | core | 8 | 1.000 |
| disambiguation | medium | 8 | 0.200 |
| distractor_robustness | core | 4 | 1.000 |
| distractor_robustness | medium | 4 | 0.200 |
| follow_through | core | 11 | 1.000 |
| follow_through | medium | 12 | 0.200 |
| multi_hop | core | 6 | 1.000 |
| multi_hop | medium | 6 | 0.200 |
| negative_knowledge | core | 2 | 1.000 |
| redundancy | core | 8 | 1.000 |
| redundancy | medium | 8 | 0.200 |
| single_hop | core | 16 | 1.000 |
| single_hop | medium | 16 | 0.200 |
| temporal | core | 12 | 1.000 |
| temporal | medium | 12 | 0.200 |

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
| abstention | core |
| aggregation | n/a |
| contradiction | medium |
| disambiguation | core |
| distractor_robustness | core |
| follow_through | core |
| multi_hop | n/a |
| negative_knowledge | n/a |
| redundancy | core |
| single_hop | medium |
| temporal | medium |

## Frontier (cost vs. quality — never a single composite)

This report intentionally emits no weighted score combining cost and quality: the weights are a business judgement, not a measurement (issue athenaeum#1523). Read the Cost and Query-quality/Utilization sections above side by side, per (probe_class, corpus_scale), and decide the tradeoff from there — the numbers below are restated unweighted, one arm's cost beside its utilization, nothing summed across them.

| probe_class | corpus_scale | arm | mean_input_tokens/turn | mean_output_tokens/turn | uid_citation_rate | distinctive_ngram_overlap |
| --- | --- | --- | --- | --- | --- | --- |
| abstention | core | native_grep | 1344.9 | 81.8 | n/a | n/a |
| abstention | core | native_index | 5838.8 | 74.3 | n/a | n/a |
| abstention | core | none | 213.7 | 91.0 | n/a | n/a |
| abstention | core | oracle | 213.7 | 62.3 | n/a | n/a |
| abstention | core | pull | 5450.9 | 105.6 | 0.000 | 0.001 |
| abstention | core | push_breadcrumb | 388.3 | 104.7 | n/a | 0.058 |
| abstention | core | push_breadcrumb_pull | 6395.5 | 119.4 | 0.000 | 0.001 |
| abstention | core | push_pages_upper_bound | 1092.7 | 68.5 | 0.000 | 0.000 |
| abstention | medium | native_grep | 1289.3 | 99.7 | n/a | n/a |
| abstention | medium | native_index | 6362.3 | 105.8 | n/a | n/a |
| abstention | medium | none | 213.7 | 75.3 | n/a | n/a |
| abstention | medium | oracle | 213.7 | 71.3 | n/a | n/a |
| abstention | medium | pull | 5122.8 | 112.3 | 0.000 | 0.013 |
| abstention | medium | push_breadcrumb | 390.7 | 143.3 | n/a | 0.049 |
| abstention | medium | push_breadcrumb_pull | 5231.3 | 121.7 | 0.020 | 0.010 |
| abstention | medium | push_pages_upper_bound | 1058.0 | 127.0 | 0.067 | 0.032 |
| aggregation | core | native_grep | 3458.0 | 226.1 | n/a | n/a |
| aggregation | core | native_index | 6362.4 | 185.8 | n/a | n/a |
| aggregation | core | none | 218.0 | 77.2 | n/a | n/a |
| aggregation | core | oracle | 1304.0 | 254.3 | 0.000 | 0.131 |
| aggregation | core | pull | 4693.9 | 173.2 | 0.000 | 0.053 |
| aggregation | core | push_breadcrumb | 395.7 | 162.0 | n/a | 0.050 |
| aggregation | core | push_breadcrumb_pull | 4515.1 | 248.1 | 0.000 | 0.062 |
| aggregation | core | push_pages_upper_bound | 1004.0 | 159.5 | 0.000 | 0.086 |
| aggregation | medium | native_grep | 3020.1 | 213.4 | n/a | n/a |
| aggregation | medium | native_index | 8612.6 | 168.2 | n/a | n/a |
| aggregation | medium | none | 218.0 | 88.7 | n/a | n/a |
| aggregation | medium | oracle | 1304.0 | 243.0 | 0.000 | 0.094 |
| aggregation | medium | pull | 5993.8 | 189.5 | 0.000 | 0.040 |
| aggregation | medium | push_breadcrumb | 396.7 | 170.5 | n/a | 0.071 |
| aggregation | medium | push_breadcrumb_pull | 5342.2 | 192.7 | 0.000 | 0.037 |
| aggregation | medium | push_pages_upper_bound | 1035.3 | 186.7 | 0.033 | 0.103 |
| contradiction | core | native_grep | 1324.6 | 108.9 | n/a | n/a |
| contradiction | core | native_index | 7049.2 | 92.3 | n/a | n/a |
| contradiction | core | none | 216.0 | 74.0 | n/a | n/a |
| contradiction | core | oracle | 401.3 | 109.2 | 0.000 | 0.097 |
| contradiction | core | pull | 3475.1 | 109.2 | 0.000 | 0.028 |
| contradiction | core | push_breadcrumb | 398.3 | 129.5 | n/a | 0.025 |
| contradiction | core | push_breadcrumb_pull | 4026.6 | 119.0 | 0.017 | 0.024 |
| contradiction | core | push_pages_upper_bound | 1104.7 | 164.8 | 0.000 | 0.037 |
| contradiction | medium | native_grep | 2055.7 | 120.9 | n/a | n/a |
| contradiction | medium | native_index | 6547.2 | 90.2 | n/a | n/a |
| contradiction | medium | none | 216.0 | 94.7 | n/a | n/a |
| contradiction | medium | oracle | 401.3 | 101.8 | 0.000 | 0.088 |
| contradiction | medium | pull | 3407.7 | 113.0 | 0.000 | 0.022 |
| contradiction | medium | push_breadcrumb | 397.0 | 150.3 | n/a | 0.042 |
| contradiction | medium | push_breadcrumb_pull | 3940.6 | 123.9 | 0.000 | 0.017 |
| contradiction | medium | push_pages_upper_bound | 1111.0 | 185.8 | 0.167 | 0.061 |
| disambiguation | core | native_grep | 1696.2 | 120.5 | n/a | n/a |
| disambiguation | core | native_index | 6438.6 | 119.9 | n/a | n/a |
| disambiguation | core | none | 212.2 | 74.2 | n/a | n/a |
| disambiguation | core | oracle | 779.0 | 154.6 | 0.000 | 0.129 |
| disambiguation | core | pull | 3066.7 | 133.2 | 0.050 | 0.058 |
| disambiguation | core | push_breadcrumb | 395.5 | 135.4 | n/a | 0.021 |
| disambiguation | core | push_breadcrumb_pull | 3611.8 | 150.9 | 0.000 | 0.038 |
| disambiguation | core | push_pages_upper_bound | 1151.5 | 170.9 | 0.250 | 0.087 |
| disambiguation | medium | native_grep | 2049.5 | 114.5 | n/a | n/a |
| disambiguation | medium | native_index | 6807.4 | 109.3 | n/a | n/a |
| disambiguation | medium | none | 212.2 | 77.4 | n/a | n/a |
| disambiguation | medium | oracle | 779.0 | 160.1 | 0.000 | 0.127 |
| disambiguation | medium | pull | 3560.5 | 125.7 | 0.000 | 0.052 |
| disambiguation | medium | push_breadcrumb | 404.0 | 160.1 | n/a | 0.009 |
| disambiguation | medium | push_breadcrumb_pull | 3678.5 | 167.3 | 0.087 | 0.034 |
| disambiguation | medium | push_pages_upper_bound | 1165.2 | 189.4 | 0.250 | 0.080 |
| distractor_robustness | core | native_grep | 2115.5 | 207.9 | n/a | n/a |
| distractor_robustness | core | native_index | 6507.8 | 134.0 | n/a | n/a |
| distractor_robustness | core | none | 210.0 | 68.8 | n/a | n/a |
| distractor_robustness | core | oracle | 621.0 | 96.8 | 0.000 | 0.131 |
| distractor_robustness | core | pull | 2880.9 | 113.7 | 0.000 | 0.087 |
| distractor_robustness | core | push_breadcrumb | 393.5 | 139.0 | n/a | 0.095 |
| distractor_robustness | core | push_breadcrumb_pull | 3564.5 | 157.9 | 0.000 | 0.019 |
| distractor_robustness | core | push_pages_upper_bound | 1068.0 | 162.2 | 0.000 | 0.069 |
| distractor_robustness | medium | native_grep | 2109.6 | 215.2 | n/a | n/a |
| distractor_robustness | medium | native_index | 7152.0 | 134.8 | n/a | n/a |
| distractor_robustness | medium | none | 210.0 | 68.2 | n/a | n/a |
| distractor_robustness | medium | oracle | 621.0 | 98.8 | 0.000 | 0.119 |
| distractor_robustness | medium | pull | 3173.2 | 112.0 | 0.000 | 0.039 |
| distractor_robustness | medium | push_breadcrumb | 387.5 | 146.2 | n/a | 0.042 |
| distractor_robustness | medium | push_breadcrumb_pull | 4162.2 | 153.2 | 0.000 | 0.027 |
| distractor_robustness | medium | push_pages_upper_bound | 1011.5 | 111.0 | 0.100 | 0.050 |
| follow_through | core | native_grep | 1368.7 | 112.2 | n/a | n/a |
| follow_through | core | native_index | 6243.9 | 97.9 | n/a | n/a |
| follow_through | core | none | 229.0 | 92.2 | n/a | n/a |
| follow_through | core | oracle | 567.2 | 138.8 | 0.000 | 0.213 |
| follow_through | core | pull | 4382.5 | 141.7 | 0.000 | 0.032 |
| follow_through | core | push_breadcrumb | 417.0 | 157.2 | n/a | 0.026 |
| follow_through | core | push_breadcrumb_pull | 5015.6 | 170.2 | 0.000 | 0.019 |
| follow_through | core | push_pages_upper_bound | 1087.5 | 145.2 | 0.000 | 0.030 |
| follow_through | medium | native_grep | 1880.5 | 125.5 | n/a | n/a |
| follow_through | medium | native_index | 6995.5 | 106.6 | n/a | n/a |
| follow_through | medium | none | 229.0 | 108.5 | n/a | n/a |
| follow_through | medium | oracle | 567.2 | 139.2 | 0.000 | 0.212 |
| follow_through | medium | pull | 5245.1 | 167.8 | 0.000 | 0.026 |
| follow_through | medium | push_breadcrumb | 413.3 | 190.2 | n/a | 0.041 |
| follow_through | medium | push_breadcrumb_pull | 5945.8 | 178.2 | 0.000 | 0.019 |
| follow_through | medium | push_pages_upper_bound | 1100.8 | 179.2 | 0.017 | 0.042 |
| multi_hop | core | native_grep | 2144.5 | 127.7 | n/a | n/a |
| multi_hop | core | native_index | 6665.1 | 139.7 | n/a | n/a |
| multi_hop | core | none | 231.7 | 76.5 | n/a | n/a |
| multi_hop | core | oracle | 690.7 | 132.5 | 0.000 | 0.082 |
| multi_hop | core | pull | 4401.9 | 120.4 | 0.000 | 0.023 |
| multi_hop | core | push_breadcrumb | 404.0 | 132.3 | n/a | 0.012 |
| multi_hop | core | push_breadcrumb_pull | 6249.5 | 153.0 | 0.000 | 0.012 |
| multi_hop | core | push_pages_upper_bound | 1061.0 | 162.3 | 0.000 | 0.026 |
| multi_hop | medium | native_grep | 2493.3 | 148.0 | n/a | n/a |
| multi_hop | medium | native_index | 7192.5 | 134.8 | n/a | n/a |
| multi_hop | medium | none | 231.7 | 79.5 | n/a | n/a |
| multi_hop | medium | oracle | 690.7 | 133.2 | 0.000 | 0.088 |
| multi_hop | medium | pull | 4862.9 | 137.2 | 0.000 | 0.019 |
| multi_hop | medium | push_breadcrumb | 405.3 | 151.3 | n/a | 0.061 |
| multi_hop | medium | push_breadcrumb_pull | 6218.4 | 167.1 | 0.000 | 0.016 |
| multi_hop | medium | push_pages_upper_bound | 1075.7 | 141.8 | 0.000 | 0.026 |
| negative_knowledge | core | native_grep | 1404.7 | 99.5 | n/a | n/a |
| negative_knowledge | core | native_index | 6142.9 | 94.5 | n/a | n/a |
| negative_knowledge | core | none | 224.0 | 134.5 | n/a | n/a |
| negative_knowledge | core | oracle | 384.0 | 168.0 | 0.000 | 0.093 |
| negative_knowledge | core | pull | 3194.2 | 168.5 | 0.000 | 0.057 |
| negative_knowledge | core | push_breadcrumb | 405.0 | 214.5 | n/a | 0.041 |
| negative_knowledge | core | push_breadcrumb_pull | 4144.0 | 133.9 | 0.000 | 0.032 |
| negative_knowledge | core | push_pages_upper_bound | 1061.0 | 125.0 | 0.000 | 0.067 |
| redundancy | core | native_grep | 1268.8 | 85.4 | n/a | n/a |
| redundancy | core | native_index | 6138.5 | 72.1 | n/a | n/a |
| redundancy | core | none | 214.8 | 53.9 | n/a | n/a |
| redundancy | core | oracle | 628.2 | 73.5 | 0.000 | 0.077 |
| redundancy | core | pull | 3061.3 | 91.2 | 0.000 | 0.032 |
| redundancy | core | push_breadcrumb | 403.8 | 137.5 | n/a | 0.013 |
| redundancy | core | push_breadcrumb_pull | 3432.6 | 109.4 | 0.025 | 0.029 |
| redundancy | core | push_pages_upper_bound | 1083.5 | 78.0 | 0.000 | 0.031 |
| redundancy | medium | native_grep | 1479.1 | 91.3 | n/a | n/a |
| redundancy | medium | native_index | 6704.5 | 86.5 | n/a | n/a |
| redundancy | medium | none | 214.8 | 50.4 | n/a | n/a |
| redundancy | medium | oracle | 628.2 | 72.5 | 0.000 | 0.058 |
| redundancy | medium | pull | 2947.1 | 88.6 | 0.000 | 0.027 |
| redundancy | medium | push_breadcrumb | 397.5 | 135.9 | n/a | 0.026 |
| redundancy | medium | push_breadcrumb_pull | 3349.8 | 109.5 | 0.038 | 0.033 |
| redundancy | medium | push_pages_upper_bound | 1119.5 | 91.0 | 0.075 | 0.031 |
| single_hop | core | native_grep | 1377.8 | 124.6 | n/a | n/a |
| single_hop | core | native_index | 6062.0 | 95.8 | n/a | n/a |
| single_hop | core | none | 215.0 | 71.6 | n/a | n/a |
| single_hop | core | oracle | 566.5 | 152.7 | 0.000 | 0.173 |
| single_hop | core | pull | 3139.1 | 143.3 | 0.000 | 0.075 |
| single_hop | core | push_breadcrumb | 397.6 | 132.9 | n/a | 0.012 |
| single_hop | core | push_breadcrumb_pull | 3558.2 | 150.7 | 0.000 | 0.048 |
| single_hop | core | push_pages_upper_bound | 1027.4 | 102.8 | 0.025 | 0.069 |
| single_hop | medium | native_grep | 1563.1 | 129.1 | n/a | n/a |
| single_hop | medium | native_index | 6465.2 | 97.0 | n/a | n/a |
| single_hop | medium | none | 215.0 | 67.1 | n/a | n/a |
| single_hop | medium | oracle | 566.5 | 151.1 | 0.000 | 0.176 |
| single_hop | medium | pull | 3115.2 | 143.3 | 0.000 | 0.063 |
| single_hop | medium | push_breadcrumb | 399.0 | 144.9 | n/a | 0.028 |
| single_hop | medium | push_breadcrumb_pull | 3844.2 | 154.1 | 0.006 | 0.042 |
| single_hop | medium | push_pages_upper_bound | 1072.5 | 110.7 | 0.088 | 0.065 |
| temporal | core | native_grep | 1354.4 | 80.8 | n/a | n/a |
| temporal | core | native_index | 6103.3 | 69.3 | n/a | n/a |
| temporal | core | none | 212.3 | 49.2 | n/a | n/a |
| temporal | core | oracle | 399.2 | 62.1 | 0.000 | 0.077 |
| temporal | core | pull | 2762.4 | 81.4 | 0.017 | 0.029 |
| temporal | core | push_breadcrumb | 399.2 | 137.5 | n/a | 0.023 |
| temporal | core | push_breadcrumb_pull | 3187.3 | 98.9 | 0.000 | 0.020 |
| temporal | core | push_pages_upper_bound | 1028.8 | 71.0 | 0.000 | 0.036 |
| temporal | medium | native_grep | 1569.8 | 84.5 | n/a | n/a |
| temporal | medium | native_index | 6493.6 | 74.6 | n/a | n/a |
| temporal | medium | none | 212.3 | 52.6 | n/a | n/a |
| temporal | medium | oracle | 399.2 | 57.7 | 0.000 | 0.085 |
| temporal | medium | pull | 2964.7 | 84.5 | 0.000 | 0.030 |
| temporal | medium | push_breadcrumb | 394.8 | 144.6 | n/a | 0.046 |
| temporal | medium | push_breadcrumb_pull | 3335.6 | 95.7 | 0.000 | 0.021 |
| temporal | medium | push_pages_upper_bound | 1047.7 | 88.5 | 0.000 | 0.042 |

## Verdict: do the free dimensions settle the question?

Observed no_call_rate is near zero (0.000) and the self-vs-topic query-overlap gap is negligible (0.012). The free dimensions alone do NOT evidence the thesis in this run — judged quality scoring is needed to distinguish the arms.
