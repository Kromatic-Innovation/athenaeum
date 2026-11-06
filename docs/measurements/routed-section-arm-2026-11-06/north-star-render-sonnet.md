<!-- SPDX-License-Identifier: Apache-2.0 -->

<!--
Rendered north-star grid report, Actions run 42174388801 (2026-11-06, `claude-sonnet-5`, 48 probes x 9 arms x core x replicates 0,1), recorded in-tree for issue athenaeum#2310 because the Actions artifact expires 30 days after the run -- the athenaeum#1854 precedent. PARTIAL: 810 of 864 cells; the run aborted on a thinking-only response the provider has no fallback for. Byte-identical to the artifact's own render apart from this comment AND one redaction -- the Actions runner's own absolute checkout root, which prefixed the traceback locator inside the abort_reason, is replaced with `<repo>/`, because public-safe-lint rejects an absolute home-rooted path in a public file and this note cannot quote the literal either. The locator's meaning -- `src/athenaeum/provider.py:617` -- is unchanged.
-->

> **PARTIAL RUN** — stopped early.
> abort_reason: ValueError: response_text: no text block found among 1 content block(s); block types were ['thinking'] (<repo>/src/athenaeum/provider.py:617)

# North-star report: free dimensions only (athenaeum#1523)

- generated: 2026-11-06T06:45:54Z
- athenaeum_version: 0.23.4
- git_sha: 5c78210c8c1c
- search_backend: vector
- hybrid: on
- relevance_floor_vector: off
- relevance_floor_fts5: off
- preflight: ok provider=api probe=pto_allowance arm=push_breadcrumb_pull recall_called=true tool_calls=1
- corpus_digest[core]: 3c5ea876688b089e
- grader_revision: athenaeum#1935
- model: claude-sonnet-5
- total rollout rows: 810
- harness failures: 6
  - turn_cap[arm=native_grep, scale=core]: 5
  - turn_cap[arm=push_breadcrumb_pull, scale=core]: 1

No LLM judge is invoked anywhere on this path — every figure below is a deterministic computation over already-captured rollout transcripts. This is a **measurement, not a regression gate**; nothing here should ever fail a build.

> **partial: 810 of 864 cells** — the grid did not finish, so every figure below is computed over the cells that did. Read the verdicts as provisional.

## Decision (design doc §7, athenaeum#1734)

**Verdict arm:** `push_breadcrumb_pull` (ruling R1) -- the shipped configuration is the ONE Athenaeum arm every condition below reads. Other Athenaeum arms still appear in the per-dimension tables further down, but never in these verdicts -- no picking the most-correct arm for conditions 1-2 and the cheapest for condition 3.

Three conditions, evaluated per scale, all against the verdict arm above: **(1)** win the relationship use case (single_hop/multi_hop/disambiguation/temporal probes targeting person/company pages), one POOLED correctness rate over the whole subset, against the better native arm's own pooled rate on the same rows; **(2)** do not lose any other current use case, class by class, skipping (and naming) any class where either side has no gradable rows; **(3)** cost per correct answer within budget of the better native arm, per class, skipping (and naming) any class with no native cost data at all (ruling R5) -- `>2.0x fail`, `<=2.0x limit`, `<=1.0x target`, `<=0.5x aspirational`, or `native-zero` (a pass) when the better native arm scored zero correct answers while the verdict arm scored at least one.

report-only classes excluded: aggregation, contradiction, negative_knowledge, unprompted_push -- excluded from conditions 2 and 3 entirely (issue athenaeum#1776); promotion is an explicit operator ruling on athenaeum#1736's thread, recorded by editing `tests.evals.corpus.CONDITION_2_ENROLLED`, never a side effect of this report.

**"Better native arm"** means two different things across these conditions, both the harshest reading available to Athenaeum: for condition 1, the native arm with the HIGHER pooled correctness rate; for condition 3, the native arm with the CHEAPER DEFINED cost per correct (a native arm that scored zero correct answers has no defined cost and is never picked as "cheaper" by that alone -- see `native-zero`).

**Cutoff scale:** `none` -- no scale at or above `medium` passed all three conditions. Failing condition per scale:

- `core`: condition 1: relationship use case not won -- 'push_breadcrumb_pull' 13/17=0.765 <= best native ('native_index') 0.765; condition 2: compared 8 classes, skipped 0; worse than native on 'abstention' (0.500 < 1.000); condition 3: cost compared 8 classes, skipped 0; worst reading is 'fail' for 'abstention' (ratio=4.811)

| scale | cutoff eligible | condition 1 | condition 2 | condition 3 | reading | all pass |
| --- | --- | --- | --- | --- | --- | --- |
| core | no | fail | fail | fail | fail | fail |

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
| abstain_unknown_client | native_grep | core | 1 | api | n/a |
| abstain_unknown_client | native_index | core | 0 | api | n/a |
| abstain_unknown_client | native_index | core | 1 | api | n/a |
| abstain_unknown_client | none | core | 0 | api | n/a |
| abstain_unknown_client | none | core | 1 | api | n/a |
| abstain_unknown_client | oracle | core | 0 | api | n/a |
| abstain_unknown_client | oracle | core | 1 | api | n/a |
| abstain_unknown_client | pull | core | 0 | api | n/a |
| abstain_unknown_client | pull | core | 1 | api | n/a |
| abstain_unknown_client | push_breadcrumb | core | 0 | api | n/a |
| abstain_unknown_client | push_breadcrumb | core | 1 | api | n/a |
| abstain_unknown_client | push_breadcrumb_pull | core | 0 | api | n/a |
| abstain_unknown_client | push_breadcrumb_pull | core | 1 | api | n/a |
| abstain_unknown_client | push_pages_upper_bound | core | 0 | api | n/a |
| abstain_unknown_client | push_pages_upper_bound | core | 1 | api | n/a |
| abstain_unknown_client | push_sections_pull | core | 0 | api | n/a |
| abstain_unknown_client | push_sections_pull | core | 1 | api | n/a |
| abstain_unknown_person | native_grep | core | 0 | api | n/a |
| abstain_unknown_person | native_grep | core | 1 | api | n/a |
| abstain_unknown_person | native_index | core | 0 | api | n/a |
| abstain_unknown_person | native_index | core | 1 | api | n/a |
| abstain_unknown_person | none | core | 0 | api | n/a |
| abstain_unknown_person | none | core | 1 | api | n/a |
| abstain_unknown_person | oracle | core | 0 | api | n/a |
| abstain_unknown_person | oracle | core | 1 | api | n/a |
| abstain_unknown_person | pull | core | 0 | api | n/a |
| abstain_unknown_person | pull | core | 1 | api | n/a |
| abstain_unknown_person | push_breadcrumb | core | 0 | api | n/a |
| abstain_unknown_person | push_breadcrumb | core | 1 | api | n/a |
| abstain_unknown_person | push_breadcrumb_pull | core | 0 | api | n/a |
| abstain_unknown_person | push_breadcrumb_pull | core | 1 | api | n/a |
| abstain_unknown_person | push_pages_upper_bound | core | 0 | api | n/a |
| abstain_unknown_person | push_pages_upper_bound | core | 1 | api | n/a |
| abstain_unknown_person | push_sections_pull | core | 0 | api | n/a |
| abstain_unknown_person | push_sections_pull | core | 1 | api | n/a |
| abstain_unknown_policy | native_grep | core | 0 | api | n/a |
| abstain_unknown_policy | native_grep | core | 1 | api | n/a |
| abstain_unknown_policy | native_index | core | 0 | api | n/a |
| abstain_unknown_policy | native_index | core | 1 | api | n/a |
| abstain_unknown_policy | none | core | 0 | api | n/a |
| abstain_unknown_policy | none | core | 1 | api | n/a |
| abstain_unknown_policy | oracle | core | 0 | api | n/a |
| abstain_unknown_policy | oracle | core | 1 | api | n/a |
| abstain_unknown_policy | pull | core | 0 | api | n/a |
| abstain_unknown_policy | pull | core | 1 | api | n/a |
| abstain_unknown_policy | push_breadcrumb | core | 0 | api | n/a |
| abstain_unknown_policy | push_breadcrumb | core | 1 | api | n/a |
| abstain_unknown_policy | push_breadcrumb_pull | core | 0 | api | n/a |
| abstain_unknown_policy | push_breadcrumb_pull | core | 1 | api | n/a |
| abstain_unknown_policy | push_pages_upper_bound | core | 0 | api | n/a |
| abstain_unknown_policy | push_pages_upper_bound | core | 1 | api | n/a |
| abstain_unknown_policy | push_sections_pull | core | 0 | api | n/a |
| abstain_unknown_policy | push_sections_pull | core | 1 | api | n/a |
| acme_billing_exception | native_grep | core | 0 | api | yes |
| acme_billing_exception | native_grep | core | 1 | api | yes |
| acme_billing_exception | native_index | core | 0 | api | yes |
| acme_billing_exception | native_index | core | 1 | api | yes |
| acme_billing_exception | none | core | 0 | api | no |
| acme_billing_exception | none | core | 1 | api | no |
| acme_billing_exception | oracle | core | 0 | api | yes |
| acme_billing_exception | oracle | core | 1 | api | yes |
| acme_billing_exception | pull | core | 0 | api | yes |
| acme_billing_exception | pull | core | 1 | api | yes |
| acme_billing_exception | push_breadcrumb | core | 0 | api | no |
| acme_billing_exception | push_breadcrumb | core | 1 | api | no |
| acme_billing_exception | push_breadcrumb_pull | core | 0 | api | yes |
| acme_billing_exception | push_breadcrumb_pull | core | 1 | api | yes |
| acme_billing_exception | push_pages_upper_bound | core | 0 | api | yes |
| acme_billing_exception | push_pages_upper_bound | core | 1 | api | yes |
| acme_billing_exception | push_sections_pull | core | 0 | api | yes |
| acme_billing_exception | push_sections_pull | core | 1 | api | yes |
| anchorline_retirement_rationale | native_grep | core | 0 | api | yes |
| anchorline_retirement_rationale | native_grep | core | 1 | api | yes |
| anchorline_retirement_rationale | native_index | core | 0 | api | yes |
| anchorline_retirement_rationale | native_index | core | 1 | api | yes |
| anchorline_retirement_rationale | none | core | 0 | api | no |
| anchorline_retirement_rationale | none | core | 1 | api | no |
| anchorline_retirement_rationale | oracle | core | 0 | api | yes |
| anchorline_retirement_rationale | oracle | core | 1 | api | yes |
| anchorline_retirement_rationale | pull | core | 0 | api | yes |
| anchorline_retirement_rationale | pull | core | 1 | api | yes |
| anchorline_retirement_rationale | push_breadcrumb | core | 0 | api | no |
| anchorline_retirement_rationale | push_breadcrumb | core | 1 | api | no |
| anchorline_retirement_rationale | push_breadcrumb_pull | core | 0 | api | yes |
| anchorline_retirement_rationale | push_breadcrumb_pull | core | 1 | api | yes |
| anchorline_retirement_rationale | push_pages_upper_bound | core | 0 | api | no |
| anchorline_retirement_rationale | push_pages_upper_bound | core | 1 | api | no |
| anchorline_retirement_rationale | push_sections_pull | core | 0 | api | yes |
| anchorline_retirement_rationale | push_sections_pull | core | 1 | api | yes |
| bluewater_terms | native_grep | core | 0 | api | yes |
| bluewater_terms | native_grep | core | 1 | api | yes |
| bluewater_terms | native_index | core | 0 | api | yes |
| bluewater_terms | native_index | core | 1 | api | yes |
| bluewater_terms | none | core | 0 | api | no |
| bluewater_terms | none | core | 1 | api | no |
| bluewater_terms | oracle | core | 0 | api | yes |
| bluewater_terms | oracle | core | 1 | api | yes |
| bluewater_terms | pull | core | 0 | api | yes |
| bluewater_terms | pull | core | 1 | api | yes |
| bluewater_terms | push_breadcrumb | core | 0 | api | no |
| bluewater_terms | push_breadcrumb | core | 1 | api | no |
| bluewater_terms | push_breadcrumb_pull | core | 0 | api | yes |
| bluewater_terms | push_breadcrumb_pull | core | 1 | api | yes |
| bluewater_terms | push_pages_upper_bound | core | 0 | api | yes |
| bluewater_terms | push_pages_upper_bound | core | 1 | api | yes |
| bluewater_terms | push_sections_pull | core | 0 | api | yes |
| bluewater_terms | push_sections_pull | core | 1 | api | yes |
| bramfield_retainer_renewal | native_grep | core | 1 | api | yes |
| bramfield_retainer_renewal | native_index | core | 1 | api | yes |
| bramfield_retainer_renewal | none | core | 1 | api | no |
| bramfield_retainer_renewal | oracle | core | 1 | api | yes |
| bramfield_retainer_renewal | pull | core | 1 | api | yes |
| bramfield_retainer_renewal | push_breadcrumb | core | 1 | api | no |
| bramfield_retainer_renewal | push_breadcrumb_pull | core | 1 | api | yes |
| bramfield_retainer_renewal | push_pages_upper_bound | core | 1 | api | no |
| bramfield_retainer_renewal | push_sections_pull | core | 1 | api | yes |
| budget_threshold_current | native_grep | core | 0 | api | yes |
| budget_threshold_current | native_grep | core | 1 | api | yes |
| budget_threshold_current | native_index | core | 0 | api | no |
| budget_threshold_current | native_index | core | 1 | api | yes |
| budget_threshold_current | none | core | 0 | api | no |
| budget_threshold_current | none | core | 1 | api | no |
| budget_threshold_current | oracle | core | 0 | api | yes |
| budget_threshold_current | oracle | core | 1 | api | yes |
| budget_threshold_current | pull | core | 0 | api | yes |
| budget_threshold_current | pull | core | 1 | api | no |
| budget_threshold_current | push_breadcrumb | core | 0 | api | no |
| budget_threshold_current | push_breadcrumb | core | 1 | api | no |
| budget_threshold_current | push_breadcrumb_pull | core | 0 | api | no |
| budget_threshold_current | push_breadcrumb_pull | core | 1 | api | no |
| budget_threshold_current | push_pages_upper_bound | core | 0 | api | yes |
| budget_threshold_current | push_pages_upper_bound | core | 1 | api | yes |
| budget_threshold_current | push_sections_pull | core | 0 | api | yes |
| budget_threshold_current | push_sections_pull | core | 1 | api | yes |
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
| callum_drews_last_contact | native_grep | core | 1 | api | yes |
| callum_drews_last_contact | native_index | core | 0 | api | yes |
| callum_drews_last_contact | native_index | core | 1 | api | yes |
| callum_drews_last_contact | none | core | 0 | api | no |
| callum_drews_last_contact | none | core | 1 | api | no |
| callum_drews_last_contact | oracle | core | 0 | api | yes |
| callum_drews_last_contact | oracle | core | 1 | api | yes |
| callum_drews_last_contact | pull | core | 0 | api | yes |
| callum_drews_last_contact | pull | core | 1 | api | yes |
| callum_drews_last_contact | push_breadcrumb | core | 0 | api | no |
| callum_drews_last_contact | push_breadcrumb | core | 1 | api | no |
| callum_drews_last_contact | push_breadcrumb_pull | core | 0 | api | yes |
| callum_drews_last_contact | push_breadcrumb_pull | core | 1 | api | yes |
| callum_drews_last_contact | push_pages_upper_bound | core | 0 | api | yes |
| callum_drews_last_contact | push_pages_upper_bound | core | 1 | api | yes |
| callum_drews_last_contact | push_sections_pull | core | 0 | api | yes |
| callum_drews_last_contact | push_sections_pull | core | 1 | api | yes |
| claims_logging_off | native_grep | core | 0 | api | yes |
| claims_logging_off | native_grep | core | 1 | api | yes |
| claims_logging_off | native_index | core | 0 | api | yes |
| claims_logging_off | native_index | core | 1 | api | yes |
| claims_logging_off | none | core | 0 | api | no |
| claims_logging_off | none | core | 1 | api | no |
| claims_logging_off | oracle | core | 0 | api | yes |
| claims_logging_off | oracle | core | 1 | api | yes |
| claims_logging_off | pull | core | 0 | api | no |
| claims_logging_off | pull | core | 1 | api | no |
| claims_logging_off | push_breadcrumb | core | 0 | api | no |
| claims_logging_off | push_breadcrumb | core | 1 | api | no |
| claims_logging_off | push_breadcrumb_pull | core | 0 | api | yes |
| claims_logging_off | push_breadcrumb_pull | core | 1 | api | yes |
| claims_logging_off | push_pages_upper_bound | core | 0 | api | no |
| claims_logging_off | push_pages_upper_bound | core | 1 | api | no |
| claims_logging_off | push_sections_pull | core | 0 | api | yes |
| claims_logging_off | push_sections_pull | core | 1 | api | yes |
| confidentiality_rule | native_grep | core | 0 | api | yes |
| confidentiality_rule | native_grep | core | 1 | api | yes |
| confidentiality_rule | native_index | core | 0 | api | no |
| confidentiality_rule | native_index | core | 1 | api | yes |
| confidentiality_rule | none | core | 0 | api | no |
| confidentiality_rule | none | core | 1 | api | no |
| confidentiality_rule | oracle | core | 0 | api | yes |
| confidentiality_rule | oracle | core | 1 | api | yes |
| confidentiality_rule | pull | core | 0 | api | yes |
| confidentiality_rule | pull | core | 1 | api | yes |
| confidentiality_rule | push_breadcrumb | core | 0 | api | no |
| confidentiality_rule | push_breadcrumb | core | 1 | api | no |
| confidentiality_rule | push_breadcrumb_pull | core | 0 | api | yes |
| confidentiality_rule | push_breadcrumb_pull | core | 1 | api | yes |
| confidentiality_rule | push_pages_upper_bound | core | 0 | api | yes |
| confidentiality_rule | push_pages_upper_bound | core | 1 | api | yes |
| confidentiality_rule | push_sections_pull | core | 0 | api | yes |
| confidentiality_rule | push_sections_pull | core | 1 | api | yes |
| connection_pool_widen | native_grep | core | 0 | api | no |
| connection_pool_widen | native_grep | core | 1 | api | yes |
| connection_pool_widen | native_index | core | 0 | api | yes |
| connection_pool_widen | native_index | core | 1 | api | yes |
| connection_pool_widen | none | core | 0 | api | no |
| connection_pool_widen | none | core | 1 | api | no |
| connection_pool_widen | oracle | core | 0 | api | yes |
| connection_pool_widen | oracle | core | 1 | api | yes |
| connection_pool_widen | pull | core | 0 | api | yes |
| connection_pool_widen | pull | core | 1 | api | yes |
| connection_pool_widen | push_breadcrumb | core | 0 | api | no |
| connection_pool_widen | push_breadcrumb | core | 1 | api | no |
| connection_pool_widen | push_breadcrumb_pull | core | 0 | api | yes |
| connection_pool_widen | push_breadcrumb_pull | core | 1 | api | yes |
| connection_pool_widen | push_pages_upper_bound | core | 0 | api | no |
| connection_pool_widen | push_pages_upper_bound | core | 1 | api | no |
| connection_pool_widen | push_sections_pull | core | 0 | api | yes |
| connection_pool_widen | push_sections_pull | core | 1 | api | yes |
| corvale_holdings_history | native_grep | core | 0 | api | yes |
| corvale_holdings_history | native_grep | core | 1 | api | yes |
| corvale_holdings_history | native_index | core | 0 | api | yes |
| corvale_holdings_history | native_index | core | 1 | api | yes |
| corvale_holdings_history | none | core | 0 | api | no |
| corvale_holdings_history | none | core | 1 | api | no |
| corvale_holdings_history | oracle | core | 0 | api | yes |
| corvale_holdings_history | oracle | core | 1 | api | yes |
| corvale_holdings_history | pull | core | 0 | api | yes |
| corvale_holdings_history | pull | core | 1 | api | yes |
| corvale_holdings_history | push_breadcrumb | core | 0 | api | no |
| corvale_holdings_history | push_breadcrumb | core | 1 | api | no |
| corvale_holdings_history | push_breadcrumb_pull | core | 0 | api | yes |
| corvale_holdings_history | push_breadcrumb_pull | core | 1 | api | yes |
| corvale_holdings_history | push_pages_upper_bound | core | 0 | api | no |
| corvale_holdings_history | push_pages_upper_bound | core | 1 | api | no |
| corvale_holdings_history | push_sections_pull | core | 0 | api | yes |
| corvale_holdings_history | push_sections_pull | core | 1 | api | yes |
| driftgate_migration_funding | native_grep | core | 0 | api | yes |
| driftgate_migration_funding | native_grep | core | 1 | api | yes |
| driftgate_migration_funding | native_index | core | 0 | api | yes |
| driftgate_migration_funding | native_index | core | 1 | api | yes |
| driftgate_migration_funding | none | core | 0 | api | no |
| driftgate_migration_funding | none | core | 1 | api | no |
| driftgate_migration_funding | oracle | core | 0 | api | yes |
| driftgate_migration_funding | oracle | core | 1 | api | yes |
| driftgate_migration_funding | pull | core | 0 | api | no |
| driftgate_migration_funding | pull | core | 1 | api | no |
| driftgate_migration_funding | push_breadcrumb | core | 0 | api | no |
| driftgate_migration_funding | push_breadcrumb | core | 1 | api | no |
| driftgate_migration_funding | push_breadcrumb_pull | core | 0 | api | no |
| driftgate_migration_funding | push_breadcrumb_pull | core | 1 | api | no |
| driftgate_migration_funding | push_pages_upper_bound | core | 0 | api | no |
| driftgate_migration_funding | push_pages_upper_bound | core | 1 | api | no |
| driftgate_migration_funding | push_sections_pull | core | 0 | api | yes |
| driftgate_migration_funding | push_sections_pull | core | 1 | api | yes |
| dual_signoff_threshold | native_grep | core | 0 | api | yes |
| dual_signoff_threshold | native_grep | core | 1 | api | no |
| dual_signoff_threshold | native_index | core | 0 | api | yes |
| dual_signoff_threshold | native_index | core | 1 | api | yes |
| dual_signoff_threshold | none | core | 0 | api | no |
| dual_signoff_threshold | none | core | 1 | api | no |
| dual_signoff_threshold | oracle | core | 0 | api | no |
| dual_signoff_threshold | oracle | core | 1 | api | no |
| dual_signoff_threshold | pull | core | 0 | api | no |
| dual_signoff_threshold | pull | core | 1 | api | no |
| dual_signoff_threshold | push_breadcrumb | core | 0 | api | no |
| dual_signoff_threshold | push_breadcrumb | core | 1 | api | no |
| dual_signoff_threshold | push_breadcrumb_pull | core | 0 | api | no |
| dual_signoff_threshold | push_breadcrumb_pull | core | 1 | api | no |
| dual_signoff_threshold | push_pages_upper_bound | core | 0 | api | no |
| dual_signoff_threshold | push_pages_upper_bound | core | 1 | api | no |
| dual_signoff_threshold | push_sections_pull | core | 0 | api | no |
| dual_signoff_threshold | push_sections_pull | core | 1 | api | no |
| export_flag_deletion | native_grep | core | 0 | api | yes |
| export_flag_deletion | native_grep | core | 1 | api | yes |
| export_flag_deletion | native_index | core | 0 | api | yes |
| export_flag_deletion | native_index | core | 1 | api | yes |
| export_flag_deletion | none | core | 0 | api | no |
| export_flag_deletion | none | core | 1 | api | no |
| export_flag_deletion | oracle | core | 0 | api | yes |
| export_flag_deletion | oracle | core | 1 | api | yes |
| export_flag_deletion | pull | core | 0 | api | yes |
| export_flag_deletion | pull | core | 1 | api | yes |
| export_flag_deletion | push_breadcrumb | core | 0 | api | no |
| export_flag_deletion | push_breadcrumb | core | 1 | api | no |
| export_flag_deletion | push_breadcrumb_pull | core | 0 | api | no |
| export_flag_deletion | push_breadcrumb_pull | core | 1 | api | no |
| export_flag_deletion | push_pages_upper_bound | core | 0 | api | no |
| export_flag_deletion | push_pages_upper_bound | core | 1 | api | no |
| export_flag_deletion | push_sections_pull | core | 0 | api | yes |
| export_flag_deletion | push_sections_pull | core | 1 | api | yes |
| fenwick_relationship_history | native_grep | core | 0 | api | yes |
| fenwick_relationship_history | native_grep | core | 1 | api | yes |
| fenwick_relationship_history | native_index | core | 0 | api | yes |
| fenwick_relationship_history | native_index | core | 1 | api | yes |
| fenwick_relationship_history | none | core | 0 | api | no |
| fenwick_relationship_history | none | core | 1 | api | no |
| fenwick_relationship_history | oracle | core | 0 | api | yes |
| fenwick_relationship_history | oracle | core | 1 | api | yes |
| fenwick_relationship_history | pull | core | 0 | api | yes |
| fenwick_relationship_history | pull | core | 1 | api | yes |
| fenwick_relationship_history | push_breadcrumb | core | 0 | api | no |
| fenwick_relationship_history | push_breadcrumb | core | 1 | api | no |
| fenwick_relationship_history | push_breadcrumb_pull | core | 0 | api | yes |
| fenwick_relationship_history | push_breadcrumb_pull | core | 1 | api | yes |
| fenwick_relationship_history | push_pages_upper_bound | core | 0 | api | no |
| fenwick_relationship_history | push_pages_upper_bound | core | 1 | api | no |
| fenwick_relationship_history | push_sections_pull | core | 0 | api | yes |
| fenwick_relationship_history | push_sections_pull | core | 1 | api | yes |
| former_client_not_current | native_grep | core | 0 | api | yes |
| former_client_not_current | native_grep | core | 1 | api | yes |
| former_client_not_current | native_index | core | 0 | api | no |
| former_client_not_current | native_index | core | 1 | api | yes |
| former_client_not_current | none | core | 0 | api | no |
| former_client_not_current | none | core | 1 | api | no |
| former_client_not_current | oracle | core | 0 | api | yes |
| former_client_not_current | oracle | core | 1 | api | yes |
| former_client_not_current | pull | core | 0 | api | yes |
| former_client_not_current | pull | core | 1 | api | yes |
| former_client_not_current | push_breadcrumb | core | 0 | api | no |
| former_client_not_current | push_breadcrumb | core | 1 | api | no |
| former_client_not_current | push_breadcrumb_pull | core | 0 | api | yes |
| former_client_not_current | push_breadcrumb_pull | core | 1 | api | yes |
| former_client_not_current | push_pages_upper_bound | core | 0 | api | yes |
| former_client_not_current | push_pages_upper_bound | core | 1 | api | yes |
| former_client_not_current | push_sections_pull | core | 0 | api | yes |
| former_client_not_current | push_sections_pull | core | 1 | api | yes |
| gilcrest_vendor_decision | native_grep | core | 0 | api | yes |
| gilcrest_vendor_decision | native_grep | core | 1 | api | yes |
| gilcrest_vendor_decision | native_index | core | 0 | api | yes |
| gilcrest_vendor_decision | native_index | core | 1 | api | yes |
| gilcrest_vendor_decision | none | core | 0 | api | no |
| gilcrest_vendor_decision | none | core | 1 | api | no |
| gilcrest_vendor_decision | oracle | core | 0 | api | yes |
| gilcrest_vendor_decision | oracle | core | 1 | api | yes |
| gilcrest_vendor_decision | pull | core | 0 | api | yes |
| gilcrest_vendor_decision | pull | core | 1 | api | yes |
| gilcrest_vendor_decision | push_breadcrumb | core | 0 | api | no |
| gilcrest_vendor_decision | push_breadcrumb | core | 1 | api | no |
| gilcrest_vendor_decision | push_breadcrumb_pull | core | 0 | api | no |
| gilcrest_vendor_decision | push_breadcrumb_pull | core | 1 | api | no |
| gilcrest_vendor_decision | push_pages_upper_bound | core | 0 | api | no |
| gilcrest_vendor_decision | push_pages_upper_bound | core | 1 | api | no |
| gilcrest_vendor_decision | push_sections_pull | core | 0 | api | yes |
| gilcrest_vendor_decision | push_sections_pull | core | 1 | api | yes |
| given_name_is_ambiguous | native_grep | core | 0 | api | no |
| given_name_is_ambiguous | native_grep | core | 1 | api | yes |
| given_name_is_ambiguous | native_index | core | 0 | api | yes |
| given_name_is_ambiguous | native_index | core | 1 | api | yes |
| given_name_is_ambiguous | none | core | 0 | api | no |
| given_name_is_ambiguous | none | core | 1 | api | no |
| given_name_is_ambiguous | oracle | core | 0 | api | yes |
| given_name_is_ambiguous | oracle | core | 1 | api | yes |
| given_name_is_ambiguous | pull | core | 0 | api | yes |
| given_name_is_ambiguous | pull | core | 1 | api | yes |
| given_name_is_ambiguous | push_breadcrumb | core | 0 | api | no |
| given_name_is_ambiguous | push_breadcrumb | core | 1 | api | no |
| given_name_is_ambiguous | push_breadcrumb_pull | core | 0 | api | yes |
| given_name_is_ambiguous | push_breadcrumb_pull | core | 1 | api | yes |
| given_name_is_ambiguous | push_pages_upper_bound | core | 0 | api | yes |
| given_name_is_ambiguous | push_pages_upper_bound | core | 1 | api | yes |
| given_name_is_ambiguous | push_sections_pull | core | 0 | api | yes |
| given_name_is_ambiguous | push_sections_pull | core | 1 | api | yes |
| invoice_role_merge | native_grep | core | 0 | api | yes |
| invoice_role_merge | native_grep | core | 1 | api | yes |
| invoice_role_merge | native_index | core | 0 | api | yes |
| invoice_role_merge | native_index | core | 1 | api | yes |
| invoice_role_merge | none | core | 0 | api | no |
| invoice_role_merge | none | core | 1 | api | no |
| invoice_role_merge | oracle | core | 0 | api | yes |
| invoice_role_merge | oracle | core | 1 | api | yes |
| invoice_role_merge | pull | core | 0 | api | yes |
| invoice_role_merge | pull | core | 1 | api | yes |
| invoice_role_merge | push_breadcrumb | core | 0 | api | no |
| invoice_role_merge | push_breadcrumb | core | 1 | api | no |
| invoice_role_merge | push_breadcrumb_pull | core | 0 | api | yes |
| invoice_role_merge | push_breadcrumb_pull | core | 1 | api | yes |
| invoice_role_merge | push_pages_upper_bound | core | 0 | api | no |
| invoice_role_merge | push_pages_upper_bound | core | 1 | api | no |
| invoice_role_merge | push_sections_pull | core | 0 | api | yes |
| invoice_role_merge | push_sections_pull | core | 1 | api | yes |
| keelbridge_programme_scope | native_grep | core | 0 | api | yes |
| keelbridge_programme_scope | native_grep | core | 1 | api | yes |
| keelbridge_programme_scope | native_index | core | 0 | api | yes |
| keelbridge_programme_scope | native_index | core | 1 | api | yes |
| keelbridge_programme_scope | none | core | 0 | api | no |
| keelbridge_programme_scope | none | core | 1 | api | no |
| keelbridge_programme_scope | oracle | core | 0 | api | yes |
| keelbridge_programme_scope | oracle | core | 1 | api | yes |
| keelbridge_programme_scope | pull | core | 0 | api | yes |
| keelbridge_programme_scope | pull | core | 1 | api | no |
| keelbridge_programme_scope | push_breadcrumb | core | 0 | api | no |
| keelbridge_programme_scope | push_breadcrumb | core | 1 | api | no |
| keelbridge_programme_scope | push_breadcrumb_pull | core | 0 | api | yes |
| keelbridge_programme_scope | push_breadcrumb_pull | core | 1 | api | yes |
| keelbridge_programme_scope | push_pages_upper_bound | core | 0 | api | no |
| keelbridge_programme_scope | push_pages_upper_bound | core | 1 | api | no |
| keelbridge_programme_scope | push_sections_pull | core | 0 | api | yes |
| keelbridge_programme_scope | push_sections_pull | core | 1 | api | yes |
| lighthouse_migration_rollback | native_grep | core | 0 | api | yes |
| lighthouse_migration_rollback | native_grep | core | 1 | api | yes |
| lighthouse_migration_rollback | native_index | core | 0 | api | yes |
| lighthouse_migration_rollback | native_index | core | 1 | api | yes |
| lighthouse_migration_rollback | none | core | 0 | api | no |
| lighthouse_migration_rollback | none | core | 1 | api | no |
| lighthouse_migration_rollback | oracle | core | 0 | api | yes |
| lighthouse_migration_rollback | oracle | core | 1 | api | yes |
| lighthouse_migration_rollback | pull | core | 0 | api | yes |
| lighthouse_migration_rollback | pull | core | 1 | api | yes |
| lighthouse_migration_rollback | push_breadcrumb | core | 0 | api | no |
| lighthouse_migration_rollback | push_breadcrumb | core | 1 | api | no |
| lighthouse_migration_rollback | push_breadcrumb_pull | core | 0 | api | yes |
| lighthouse_migration_rollback | push_breadcrumb_pull | core | 1 | api | yes |
| lighthouse_migration_rollback | push_pages_upper_bound | core | 0 | api | no |
| lighthouse_migration_rollback | push_pages_upper_bound | core | 1 | api | no |
| lighthouse_migration_rollback | push_sections_pull | core | 0 | api | yes |
| lighthouse_migration_rollback | push_sections_pull | core | 1 | api | yes |
| migration_spot_check_cut | native_grep | core | 0 | api | yes |
| migration_spot_check_cut | native_grep | core | 1 | api | yes |
| migration_spot_check_cut | native_index | core | 0 | api | yes |
| migration_spot_check_cut | native_index | core | 1 | api | yes |
| migration_spot_check_cut | none | core | 0 | api | no |
| migration_spot_check_cut | none | core | 1 | api | no |
| migration_spot_check_cut | oracle | core | 0 | api | yes |
| migration_spot_check_cut | oracle | core | 1 | api | yes |
| migration_spot_check_cut | pull | core | 0 | api | yes |
| migration_spot_check_cut | pull | core | 1 | api | yes |
| migration_spot_check_cut | push_breadcrumb | core | 0 | api | no |
| migration_spot_check_cut | push_breadcrumb | core | 1 | api | no |
| migration_spot_check_cut | push_breadcrumb_pull | core | 0 | api | yes |
| migration_spot_check_cut | push_breadcrumb_pull | core | 1 | api | yes |
| migration_spot_check_cut | push_pages_upper_bound | core | 0 | api | no |
| migration_spot_check_cut | push_pages_upper_bound | core | 1 | api | no |
| migration_spot_check_cut | push_sections_pull | core | 0 | api | no |
| migration_spot_check_cut | push_sections_pull | core | 1 | api | yes |
| mira_castellane_role | native_grep | core | 0 | api | no |
| mira_castellane_role | native_grep | core | 1 | api | no |
| mira_castellane_role | native_index | core | 0 | api | yes |
| mira_castellane_role | native_index | core | 1 | api | yes |
| mira_castellane_role | none | core | 0 | api | no |
| mira_castellane_role | none | core | 1 | api | no |
| mira_castellane_role | oracle | core | 0 | api | yes |
| mira_castellane_role | oracle | core | 1 | api | yes |
| mira_castellane_role | pull | core | 0 | api | no |
| mira_castellane_role | pull | core | 1 | api | no |
| mira_castellane_role | push_breadcrumb | core | 0 | api | no |
| mira_castellane_role | push_breadcrumb | core | 1 | api | no |
| mira_castellane_role | push_breadcrumb_pull | core | 0 | api | no |
| mira_castellane_role | push_breadcrumb_pull | core | 1 | api | no |
| mira_castellane_role | push_pages_upper_bound | core | 0 | api | yes |
| mira_castellane_role | push_pages_upper_bound | core | 1 | api | yes |
| mira_castellane_role | push_sections_pull | core | 0 | api | yes |
| mira_castellane_role | push_sections_pull | core | 1 | api | yes |
| nightly_job_drop | native_grep | core | 0 | api | yes |
| nightly_job_drop | native_grep | core | 1 | api | yes |
| nightly_job_drop | native_index | core | 0 | api | yes |
| nightly_job_drop | native_index | core | 1 | api | yes |
| nightly_job_drop | none | core | 0 | api | no |
| nightly_job_drop | none | core | 1 | api | no |
| nightly_job_drop | oracle | core | 0 | api | yes |
| nightly_job_drop | oracle | core | 1 | api | yes |
| nightly_job_drop | pull | core | 0 | api | no |
| nightly_job_drop | pull | core | 1 | api | no |
| nightly_job_drop | push_breadcrumb | core | 0 | api | no |
| nightly_job_drop | push_breadcrumb | core | 1 | api | no |
| nightly_job_drop | push_breadcrumb_pull | core | 0 | api | no |
| nightly_job_drop | push_breadcrumb_pull | core | 1 | api | yes |
| nightly_job_drop | push_pages_upper_bound | core | 0 | api | no |
| nightly_job_drop | push_pages_upper_bound | core | 1 | api | no |
| nightly_job_drop | push_sections_pull | core | 0 | api | no |
| nightly_job_drop | push_sections_pull | core | 1 | api | no |
| office_address_current | native_grep | core | 0 | api | yes |
| office_address_current | native_grep | core | 1 | api | yes |
| office_address_current | native_index | core | 0 | api | yes |
| office_address_current | native_index | core | 1 | api | yes |
| office_address_current | none | core | 0 | api | no |
| office_address_current | none | core | 1 | api | no |
| office_address_current | oracle | core | 0 | api | yes |
| office_address_current | oracle | core | 1 | api | yes |
| office_address_current | pull | core | 0 | api | yes |
| office_address_current | pull | core | 1 | api | yes |
| office_address_current | push_breadcrumb | core | 0 | api | no |
| office_address_current | push_breadcrumb | core | 1 | api | no |
| office_address_current | push_breadcrumb_pull | core | 0 | api | yes |
| office_address_current | push_breadcrumb_pull | core | 1 | api | yes |
| office_address_current | push_pages_upper_bound | core | 0 | api | yes |
| office_address_current | push_pages_upper_bound | core | 1 | api | yes |
| office_address_current | push_sections_pull | core | 0 | api | yes |
| office_address_current | push_sections_pull | core | 1 | api | yes |
| onboarding_length | native_grep | core | 0 | api | yes |
| onboarding_length | native_grep | core | 1 | api | yes |
| onboarding_length | native_index | core | 0 | api | yes |
| onboarding_length | native_index | core | 1 | api | yes |
| onboarding_length | none | core | 0 | api | no |
| onboarding_length | none | core | 1 | api | no |
| onboarding_length | oracle | core | 0 | api | yes |
| onboarding_length | oracle | core | 1 | api | yes |
| onboarding_length | pull | core | 0 | api | yes |
| onboarding_length | pull | core | 1 | api | yes |
| onboarding_length | push_breadcrumb | core | 0 | api | no |
| onboarding_length | push_breadcrumb | core | 1 | api | no |
| onboarding_length | push_breadcrumb_pull | core | 0 | api | yes |
| onboarding_length | push_breadcrumb_pull | core | 1 | api | yes |
| onboarding_length | push_pages_upper_bound | core | 0 | api | yes |
| onboarding_length | push_pages_upper_bound | core | 1 | api | yes |
| onboarding_length | push_sections_pull | core | 0 | api | yes |
| onboarding_length | push_sections_pull | core | 1 | api | yes |
| person_not_repo | native_grep | core | 0 | api | no |
| person_not_repo | native_grep | core | 1 | api | no |
| person_not_repo | native_index | core | 0 | api | yes |
| person_not_repo | native_index | core | 1 | api | yes |
| person_not_repo | none | core | 0 | api | no |
| person_not_repo | none | core | 1 | api | no |
| person_not_repo | oracle | core | 0 | api | yes |
| person_not_repo | oracle | core | 1 | api | yes |
| person_not_repo | pull | core | 0 | api | no |
| person_not_repo | pull | core | 1 | api | no |
| person_not_repo | push_breadcrumb | core | 0 | api | no |
| person_not_repo | push_breadcrumb | core | 1 | api | no |
| person_not_repo | push_breadcrumb_pull | core | 0 | api | yes |
| person_not_repo | push_breadcrumb_pull | core | 1 | api | yes |
| person_not_repo | push_pages_upper_bound | core | 0 | api | no |
| person_not_repo | push_pages_upper_bound | core | 1 | api | no |
| person_not_repo | push_sections_pull | core | 0 | api | yes |
| person_not_repo | push_sections_pull | core | 1 | api | yes |
| pinemarsh_goal_rationale | native_grep | core | 0 | api | yes |
| pinemarsh_goal_rationale | native_grep | core | 1 | api | yes |
| pinemarsh_goal_rationale | native_index | core | 0 | api | yes |
| pinemarsh_goal_rationale | native_index | core | 1 | api | yes |
| pinemarsh_goal_rationale | none | core | 0 | api | no |
| pinemarsh_goal_rationale | none | core | 1 | api | no |
| pinemarsh_goal_rationale | oracle | core | 0 | api | yes |
| pinemarsh_goal_rationale | oracle | core | 1 | api | yes |
| pinemarsh_goal_rationale | pull | core | 0 | api | yes |
| pinemarsh_goal_rationale | pull | core | 1 | api | yes |
| pinemarsh_goal_rationale | push_breadcrumb | core | 0 | api | no |
| pinemarsh_goal_rationale | push_breadcrumb | core | 1 | api | no |
| pinemarsh_goal_rationale | push_breadcrumb_pull | core | 0 | api | yes |
| pinemarsh_goal_rationale | push_breadcrumb_pull | core | 1 | api | yes |
| pinemarsh_goal_rationale | push_pages_upper_bound | core | 0 | api | no |
| pinemarsh_goal_rationale | push_pages_upper_bound | core | 1 | api | no |
| pinemarsh_goal_rationale | push_sections_pull | core | 0 | api | yes |
| pinemarsh_goal_rationale | push_sections_pull | core | 1 | api | yes |
| portal_design_reviewer | native_grep | core | 0 | api | no |
| portal_design_reviewer | native_grep | core | 1 | api | yes |
| portal_design_reviewer | native_index | core | 0 | api | yes |
| portal_design_reviewer | native_index | core | 1 | api | yes |
| portal_design_reviewer | none | core | 0 | api | no |
| portal_design_reviewer | none | core | 1 | api | no |
| portal_design_reviewer | oracle | core | 0 | api | yes |
| portal_design_reviewer | oracle | core | 1 | api | yes |
| portal_design_reviewer | pull | core | 0 | api | yes |
| portal_design_reviewer | pull | core | 1 | api | yes |
| portal_design_reviewer | push_breadcrumb | core | 0 | api | no |
| portal_design_reviewer | push_breadcrumb | core | 1 | api | no |
| portal_design_reviewer | push_breadcrumb_pull | core | 0 | api | yes |
| portal_design_reviewer | push_breadcrumb_pull | core | 1 | api | yes |
| portal_design_reviewer | push_pages_upper_bound | core | 0 | api | no |
| portal_design_reviewer | push_pages_upper_bound | core | 1 | api | no |
| portal_design_reviewer | push_sections_pull | core | 0 | api | yes |
| portal_design_reviewer | push_sections_pull | core | 1 | api | yes |
| pto_allowance | native_grep | core | 0 | api | yes |
| pto_allowance | native_grep | core | 1 | api | yes |
| pto_allowance | native_index | core | 0 | api | no |
| pto_allowance | native_index | core | 1 | api | no |
| pto_allowance | none | core | 0 | api | no |
| pto_allowance | none | core | 1 | api | no |
| pto_allowance | oracle | core | 0 | api | yes |
| pto_allowance | oracle | core | 1 | api | yes |
| pto_allowance | pull | core | 0 | api | yes |
| pto_allowance | pull | core | 1 | api | yes |
| pto_allowance | push_breadcrumb | core | 0 | api | no |
| pto_allowance | push_breadcrumb | core | 1 | api | no |
| pto_allowance | push_breadcrumb_pull | core | 0 | api | yes |
| pto_allowance | push_breadcrumb_pull | core | 1 | api | yes |
| pto_allowance | push_pages_upper_bound | core | 0 | api | yes |
| pto_allowance | push_pages_upper_bound | core | 1 | api | yes |
| pto_allowance | push_sections_pull | core | 0 | api | yes |
| pto_allowance | push_sections_pull | core | 1 | api | yes |
| rate_limit_removal | native_grep | core | 0 | api | yes |
| rate_limit_removal | native_grep | core | 1 | api | yes |
| rate_limit_removal | native_index | core | 0 | api | yes |
| rate_limit_removal | native_index | core | 1 | api | yes |
| rate_limit_removal | none | core | 0 | api | no |
| rate_limit_removal | none | core | 1 | api | no |
| rate_limit_removal | oracle | core | 0 | api | yes |
| rate_limit_removal | oracle | core | 1 | api | yes |
| rate_limit_removal | pull | core | 0 | api | yes |
| rate_limit_removal | pull | core | 1 | api | yes |
| rate_limit_removal | push_breadcrumb | core | 0 | api | no |
| rate_limit_removal | push_breadcrumb | core | 1 | api | no |
| rate_limit_removal | push_breadcrumb_pull | core | 0 | api | yes |
| rate_limit_removal | push_breadcrumb_pull | core | 1 | api | yes |
| rate_limit_removal | push_pages_upper_bound | core | 0 | api | no |
| rate_limit_removal | push_pages_upper_bound | core | 1 | api | no |
| rate_limit_removal | push_sections_pull | core | 0 | api | yes |
| rate_limit_removal | push_sections_pull | core | 1 | api | yes |
| ratecard_tooling_owner | native_grep | core | 0 | api | no |
| ratecard_tooling_owner | native_grep | core | 1 | api | no |
| ratecard_tooling_owner | native_index | core | 0 | api | yes |
| ratecard_tooling_owner | native_index | core | 1 | api | yes |
| ratecard_tooling_owner | none | core | 0 | api | no |
| ratecard_tooling_owner | none | core | 1 | api | no |
| ratecard_tooling_owner | oracle | core | 0 | api | yes |
| ratecard_tooling_owner | oracle | core | 1 | api | yes |
| ratecard_tooling_owner | pull | core | 0 | api | yes |
| ratecard_tooling_owner | pull | core | 1 | api | yes |
| ratecard_tooling_owner | push_breadcrumb | core | 0 | api | no |
| ratecard_tooling_owner | push_breadcrumb | core | 1 | api | no |
| ratecard_tooling_owner | push_breadcrumb_pull | core | 0 | api | yes |
| ratecard_tooling_owner | push_breadcrumb_pull | core | 1 | api | no |
| ratecard_tooling_owner | push_pages_upper_bound | core | 0 | api | no |
| ratecard_tooling_owner | push_pages_upper_bound | core | 1 | api | no |
| ratecard_tooling_owner | push_sections_pull | core | 0 | api | yes |
| ratecard_tooling_owner | push_sections_pull | core | 1 | api | yes |
| relay_sync_fix | native_grep | core | 1 | api | yes |
| relay_sync_fix | native_index | core | 1 | api | yes |
| relay_sync_fix | none | core | 1 | api | no |
| relay_sync_fix | oracle | core | 1 | api | yes |
| relay_sync_fix | pull | core | 1 | api | yes |
| relay_sync_fix | push_breadcrumb | core | 1 | api | no |
| relay_sync_fix | push_breadcrumb_pull | core | 1 | api | yes |
| relay_sync_fix | push_pages_upper_bound | core | 1 | api | no |
| relay_sync_fix | push_sections_pull | core | 1 | api | yes |
| remote_equipment_stipend_cap | native_grep | core | 0 | api | yes |
| remote_equipment_stipend_cap | native_grep | core | 1 | api | yes |
| remote_equipment_stipend_cap | native_index | core | 0 | api | yes |
| remote_equipment_stipend_cap | native_index | core | 1 | api | yes |
| remote_equipment_stipend_cap | none | core | 0 | api | no |
| remote_equipment_stipend_cap | none | core | 1 | api | no |
| remote_equipment_stipend_cap | oracle | core | 0 | api | yes |
| remote_equipment_stipend_cap | oracle | core | 1 | api | yes |
| remote_equipment_stipend_cap | pull | core | 0 | api | yes |
| remote_equipment_stipend_cap | pull | core | 1 | api | yes |
| remote_equipment_stipend_cap | push_breadcrumb | core | 0 | api | no |
| remote_equipment_stipend_cap | push_breadcrumb | core | 1 | api | no |
| remote_equipment_stipend_cap | push_breadcrumb_pull | core | 0 | api | yes |
| remote_equipment_stipend_cap | push_breadcrumb_pull | core | 1 | api | yes |
| remote_equipment_stipend_cap | push_pages_upper_bound | core | 0 | api | no |
| remote_equipment_stipend_cap | push_pages_upper_bound | core | 1 | api | no |
| remote_equipment_stipend_cap | push_sections_pull | core | 0 | api | yes |
| remote_equipment_stipend_cap | push_sections_pull | core | 1 | api | yes |
| repo_not_person | native_grep | core | 0 | api | yes |
| repo_not_person | native_grep | core | 1 | api | yes |
| repo_not_person | native_index | core | 0 | api | yes |
| repo_not_person | native_index | core | 1 | api | yes |
| repo_not_person | none | core | 0 | api | no |
| repo_not_person | none | core | 1 | api | no |
| repo_not_person | oracle | core | 0 | api | yes |
| repo_not_person | oracle | core | 1 | api | yes |
| repo_not_person | pull | core | 0 | api | no |
| repo_not_person | pull | core | 1 | api | no |
| repo_not_person | push_breadcrumb | core | 0 | api | no |
| repo_not_person | push_breadcrumb | core | 1 | api | no |
| repo_not_person | push_breadcrumb_pull | core | 0 | api | no |
| repo_not_person | push_breadcrumb_pull | core | 1 | api | no |
| repo_not_person | push_pages_upper_bound | core | 0 | api | no |
| repo_not_person | push_pages_upper_bound | core | 1 | api | no |
| repo_not_person | push_sections_pull | core | 0 | api | yes |
| repo_not_person | push_sections_pull | core | 1 | api | yes |
| spend_approver_named | native_grep | core | 0 | api | yes |
| spend_approver_named | native_grep | core | 1 | api | yes |
| spend_approver_named | native_index | core | 0 | api | yes |
| spend_approver_named | native_index | core | 1 | api | yes |
| spend_approver_named | none | core | 0 | api | no |
| spend_approver_named | none | core | 1 | api | no |
| spend_approver_named | oracle | core | 0 | api | yes |
| spend_approver_named | oracle | core | 1 | api | yes |
| spend_approver_named | pull | core | 0 | api | yes |
| spend_approver_named | pull | core | 1 | api | yes |
| spend_approver_named | push_breadcrumb | core | 0 | api | no |
| spend_approver_named | push_breadcrumb | core | 1 | api | no |
| spend_approver_named | push_breadcrumb_pull | core | 0 | api | yes |
| spend_approver_named | push_breadcrumb_pull | core | 1 | api | yes |
| spend_approver_named | push_pages_upper_bound | core | 0 | api | no |
| spend_approver_named | push_pages_upper_bound | core | 1 | api | no |
| spend_approver_named | push_sections_pull | core | 0 | api | yes |
| spend_approver_named | push_sections_pull | core | 1 | api | yes |
| standup_time_current | native_grep | core | 0 | api | yes |
| standup_time_current | native_grep | core | 1 | api | yes |
| standup_time_current | native_index | core | 0 | api | yes |
| standup_time_current | native_index | core | 1 | api | no |
| standup_time_current | none | core | 0 | api | no |
| standup_time_current | none | core | 1 | api | no |
| standup_time_current | oracle | core | 0 | api | yes |
| standup_time_current | oracle | core | 1 | api | yes |
| standup_time_current | pull | core | 0 | api | yes |
| standup_time_current | pull | core | 1 | api | yes |
| standup_time_current | push_breadcrumb | core | 0 | api | no |
| standup_time_current | push_breadcrumb | core | 1 | api | no |
| standup_time_current | push_breadcrumb_pull | core | 0 | api | yes |
| standup_time_current | push_breadcrumb_pull | core | 1 | api | yes |
| standup_time_current | push_pages_upper_bound | core | 0 | api | yes |
| standup_time_current | push_pages_upper_bound | core | 1 | api | yes |
| standup_time_current | push_sections_pull | core | 0 | api | yes |
| standup_time_current | push_sections_pull | core | 1 | api | yes |
| subsidiary_onboarding_lesson | native_grep | core | 0 | api | yes |
| subsidiary_onboarding_lesson | native_grep | core | 1 | api | yes |
| subsidiary_onboarding_lesson | native_index | core | 0 | api | yes |
| subsidiary_onboarding_lesson | native_index | core | 1 | api | yes |
| subsidiary_onboarding_lesson | none | core | 0 | api | no |
| subsidiary_onboarding_lesson | none | core | 1 | api | no |
| subsidiary_onboarding_lesson | oracle | core | 0 | api | yes |
| subsidiary_onboarding_lesson | oracle | core | 1 | api | yes |
| subsidiary_onboarding_lesson | pull | core | 0 | api | yes |
| subsidiary_onboarding_lesson | pull | core | 1 | api | yes |
| subsidiary_onboarding_lesson | push_breadcrumb | core | 0 | api | no |
| subsidiary_onboarding_lesson | push_breadcrumb | core | 1 | api | no |
| subsidiary_onboarding_lesson | push_breadcrumb_pull | core | 0 | api | yes |
| subsidiary_onboarding_lesson | push_breadcrumb_pull | core | 1 | api | yes |
| subsidiary_onboarding_lesson | push_pages_upper_bound | core | 0 | api | no |
| subsidiary_onboarding_lesson | push_pages_upper_bound | core | 1 | api | no |
| subsidiary_onboarding_lesson | push_sections_pull | core | 0 | api | yes |
| subsidiary_onboarding_lesson | push_sections_pull | core | 1 | api | yes |
| surname_is_ambiguous | native_grep | core | 0 | api | yes |
| surname_is_ambiguous | native_index | core | 0 | api | yes |
| surname_is_ambiguous | none | core | 0 | api | no |
| surname_is_ambiguous | oracle | core | 0 | api | yes |
| surname_is_ambiguous | pull | core | 0 | api | no |
| surname_is_ambiguous | push_breadcrumb | core | 0 | api | no |
| surname_is_ambiguous | push_breadcrumb_pull | core | 0 | api | yes |
| surname_is_ambiguous | push_pages_upper_bound | core | 0 | api | yes |
| surname_is_ambiguous | push_sections_pull | core | 0 | api | yes |
| tamsin_ferro_role_change | native_grep | core | 0 | api | yes |
| tamsin_ferro_role_change | native_grep | core | 1 | api | yes |
| tamsin_ferro_role_change | native_index | core | 0 | api | yes |
| tamsin_ferro_role_change | native_index | core | 1 | api | yes |
| tamsin_ferro_role_change | none | core | 0 | api | no |
| tamsin_ferro_role_change | none | core | 1 | api | no |
| tamsin_ferro_role_change | oracle | core | 0 | api | yes |
| tamsin_ferro_role_change | oracle | core | 1 | api | yes |
| tamsin_ferro_role_change | pull | core | 0 | api | yes |
| tamsin_ferro_role_change | pull | core | 1 | api | yes |
| tamsin_ferro_role_change | push_breadcrumb | core | 0 | api | no |
| tamsin_ferro_role_change | push_breadcrumb | core | 1 | api | no |
| tamsin_ferro_role_change | push_breadcrumb_pull | core | 0 | api | yes |
| tamsin_ferro_role_change | push_breadcrumb_pull | core | 1 | api | yes |
| tamsin_ferro_role_change | push_pages_upper_bound | core | 0 | api | yes |
| tamsin_ferro_role_change | push_pages_upper_bound | core | 1 | api | yes |
| tamsin_ferro_role_change | push_sections_pull | core | 0 | api | yes |
| tamsin_ferro_role_change | push_sections_pull | core | 1 | api | yes |
| thorncastle_first_contact | native_grep | core | 0 | api | yes |
| thorncastle_first_contact | native_grep | core | 1 | api | yes |
| thorncastle_first_contact | native_index | core | 0 | api | yes |
| thorncastle_first_contact | native_index | core | 1 | api | yes |
| thorncastle_first_contact | none | core | 0 | api | no |
| thorncastle_first_contact | none | core | 1 | api | no |
| thorncastle_first_contact | oracle | core | 0 | api | yes |
| thorncastle_first_contact | oracle | core | 1 | api | yes |
| thorncastle_first_contact | pull | core | 0 | api | yes |
| thorncastle_first_contact | pull | core | 1 | api | yes |
| thorncastle_first_contact | push_breadcrumb | core | 0 | api | no |
| thorncastle_first_contact | push_breadcrumb | core | 1 | api | no |
| thorncastle_first_contact | push_breadcrumb_pull | core | 0 | api | yes |
| thorncastle_first_contact | push_breadcrumb_pull | core | 1 | api | yes |
| thorncastle_first_contact | push_pages_upper_bound | core | 0 | api | yes |
| thorncastle_first_contact | push_pages_upper_bound | core | 1 | api | yes |
| thorncastle_first_contact | push_sections_pull | core | 0 | api | yes |
| thorncastle_first_contact | push_sections_pull | core | 1 | api | yes |
| tls_library_bump | native_grep | core | 0 | api | yes |
| tls_library_bump | native_grep | core | 1 | api | yes |
| tls_library_bump | native_index | core | 0 | api | yes |
| tls_library_bump | native_index | core | 1 | api | yes |
| tls_library_bump | none | core | 0 | api | no |
| tls_library_bump | none | core | 1 | api | no |
| tls_library_bump | oracle | core | 0 | api | yes |
| tls_library_bump | oracle | core | 1 | api | yes |
| tls_library_bump | pull | core | 0 | api | yes |
| tls_library_bump | pull | core | 1 | api | yes |
| tls_library_bump | push_breadcrumb | core | 0 | api | no |
| tls_library_bump | push_breadcrumb | core | 1 | api | no |
| tls_library_bump | push_breadcrumb_pull | core | 0 | api | yes |
| tls_library_bump | push_breadcrumb_pull | core | 1 | api | yes |
| tls_library_bump | push_pages_upper_bound | core | 0 | api | no |
| tls_library_bump | push_pages_upper_bound | core | 1 | api | no |
| tls_library_bump | push_sections_pull | core | 0 | api | yes |
| tls_library_bump | push_sections_pull | core | 1 | api | yes |
| triform_vendor_consolidation | native_grep | core | 0 | api | yes |
| triform_vendor_consolidation | native_grep | core | 1 | api | yes |
| triform_vendor_consolidation | native_index | core | 0 | api | yes |
| triform_vendor_consolidation | native_index | core | 1 | api | yes |
| triform_vendor_consolidation | none | core | 0 | api | no |
| triform_vendor_consolidation | none | core | 1 | api | no |
| triform_vendor_consolidation | oracle | core | 0 | api | yes |
| triform_vendor_consolidation | oracle | core | 1 | api | yes |
| triform_vendor_consolidation | pull | core | 0 | api | yes |
| triform_vendor_consolidation | pull | core | 1 | api | yes |
| triform_vendor_consolidation | push_breadcrumb | core | 0 | api | no |
| triform_vendor_consolidation | push_breadcrumb | core | 1 | api | no |
| triform_vendor_consolidation | push_breadcrumb_pull | core | 0 | api | yes |
| triform_vendor_consolidation | push_breadcrumb_pull | core | 1 | api | yes |
| triform_vendor_consolidation | push_pages_upper_bound | core | 0 | api | no |
| triform_vendor_consolidation | push_pages_upper_bound | core | 1 | api | no |
| triform_vendor_consolidation | push_sections_pull | core | 0 | api | yes |
| triform_vendor_consolidation | push_sections_pull | core | 1 | api | yes |
| webhook_backoff_lower | native_grep | core | 0 | api | yes |
| webhook_backoff_lower | native_grep | core | 1 | api | yes |
| webhook_backoff_lower | native_index | core | 0 | api | yes |
| webhook_backoff_lower | native_index | core | 1 | api | yes |
| webhook_backoff_lower | none | core | 0 | api | no |
| webhook_backoff_lower | none | core | 1 | api | no |
| webhook_backoff_lower | oracle | core | 0 | api | yes |
| webhook_backoff_lower | oracle | core | 1 | api | yes |
| webhook_backoff_lower | pull | core | 0 | api | yes |
| webhook_backoff_lower | pull | core | 1 | api | yes |
| webhook_backoff_lower | push_breadcrumb | core | 0 | api | no |
| webhook_backoff_lower | push_breadcrumb | core | 1 | api | no |
| webhook_backoff_lower | push_breadcrumb_pull | core | 0 | api | yes |
| webhook_backoff_lower | push_breadcrumb_pull | core | 1 | api | yes |
| webhook_backoff_lower | push_pages_upper_bound | core | 0 | api | no |
| webhook_backoff_lower | push_pages_upper_bound | core | 1 | api | no |
| webhook_backoff_lower | push_sections_pull | core | 0 | api | yes |
| webhook_backoff_lower | push_sections_pull | core | 1 | api | yes |

## PULL no-call rate (first-class result)

`no_call_rate = count(recall_called=False) / count(PULL rollouts)`, per (probe_class, corpus_scale). The thesis under test predicts a non-trivial rate; a rate near 0.0 across every group means the free data do NOT evidence it.

| probe_class | corpus_scale | n | no_call_rate |
| --- | --- | --- | --- |
| abstention | core | 6 | 0.000 |
| disambiguation | core | 7 | 0.000 |
| distractor_robustness | core | 4 | 0.000 |
| follow_through | core | 12 | 0.000 |
| multi_hop | core | 6 | 0.000 |
| redundancy | core | 8 | 0.000 |
| single_hop | core | 15 | 0.000 |
| temporal | core | 12 | 0.000 |
| unprompted_push | core | 20 | 0.150 |

## Cost per correct answer (athenaeum#1734)

`cost_per_correct = (read_input_tokens + read_output_tokens + write_tokens_amortized) / correct_n` -- read cost only in Phase 1. Phase 2 adds write cost amortised over the full probe set at that scale (design doc §6); for a non-native arm `write_tokens_amortized` is the native writer's own amortised share PLUS the librarian's filing share (athenaeum#1870, ruling athenaeum#1830), never the filing share alone -- `write_native_share_amortized` prints that native portion separately so neither component is hidden, and the raw (unamortised) sum is printed alongside. `n/a` in the native-share columns means either no write data at all, or (for a native arm) that the whole combined figure already is native, so there is no separate share to print; `not-captured` means an Athenaeum arm's native share specifically was never captured, so the figure shown is filing-only -- see the design doc. `undefined` (never `inf`/`0`) means zero correct answers in the cell -- there is no denominator.

`cache_weighted_tokens` (athenaeum#2305) folds cache activity into `cost_per_correct`'s numerator at the same multipliers athenaeum#239 already bills real cache activity at (a cache write = 1.25x a plain input token, a cache read = 0.10x) -- counting a PUSH/ORACLE arm's injected context on EVERY later turn it stays resident in the model's context, which a plain input+output-tokens view would otherwise price at zero past the first turn. `n/a` means no row in this group ever recorded cache usage (a pre-athenaeum#2305 row), never a measured zero.

`injected_bytes` (athenaeum#2305) is the summed UTF-8 byte length of the content actually injected for this cell's rows (PUSH/ORACLE arms only) -- reported alongside cost, never folded into `cost_per_correct`'s arithmetic. `n/a` means no row in this group recorded it.

| probe_class | corpus_scale | arm | n | correct_n | read_tokens | cache_weighted_tokens | cost_per_correct | injected_bytes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| abstention | core | native_grep | 6 | 6 | 30252 | 0.0 | 5042.0 | n/a |
| abstention | core | native_index | 6 | 2 | 95838 | 0.0 | 47919.0 | n/a |
| abstention | core | none | 6 | 6 | 1969 | 0.0 | 328.2 | n/a |
| abstention | core | oracle | 6 | 6 | 1960 | 0.0 | 326.7 | 0 |
| abstention | core | pull | 6 | 6 | 94563 | 0.0 | 15760.5 | n/a |
| abstention | core | push_breadcrumb | 6 | 6 | 4060 | 0.0 | 676.7 | 3462 |
| abstention | core | push_breadcrumb_pull | 6 | 3 | 72766 | 0.0 | 24255.3 | 3462 |
| abstention | core | push_pages_upper_bound | 6 | 6 | 9365 | 0.0 | 1560.8 | 17636 |
| abstention | core | push_sections_pull | 6 | 5 | 49696 | 0.0 | 9939.2 | 11796 |
| disambiguation | core | native_grep | 7 | 4 | 57825 | 0.0 | 14456.2 | n/a |
| disambiguation | core | native_index | 7 | 7 | 237680 | 0.0 | 33954.3 | n/a |
| disambiguation | core | none | 7 | 0 | 2313 | 0.0 | undefined | n/a |
| disambiguation | core | oracle | 7 | 7 | 8230 | 0.0 | 1175.7 | 11242 |
| disambiguation | core | pull | 7 | 7 | 55472 | 0.0 | 7924.6 | n/a |
| disambiguation | core | push_breadcrumb | 7 | 0 | 5316 | 0.0 | undefined | 3859 |
| disambiguation | core | push_breadcrumb_pull | 7 | 7 | 82345 | 0.0 | 11763.6 | 3859 |
| disambiguation | core | push_pages_upper_bound | 7 | 7 | 12529 | 0.0 | 1789.9 | 20943 |
| disambiguation | core | push_sections_pull | 7 | 7 | 33200 | 0.0 | 4742.9 | 16593 |
| distractor_robustness | core | native_grep | 4 | 3 | 38871 | 0.0 | 12957.0 | n/a |
| distractor_robustness | core | native_index | 4 | 3 | 122443 | 0.0 | 40814.3 | n/a |
| distractor_robustness | core | none | 4 | 0 | 1379 | 0.0 | undefined | n/a |
| distractor_robustness | core | oracle | 4 | 4 | 3659 | 0.0 | 914.8 | 4752 |
| distractor_robustness | core | pull | 4 | 4 | 56150 | 0.0 | 14037.5 | n/a |
| distractor_robustness | core | push_breadcrumb | 4 | 0 | 3472 | 0.0 | undefined | 2320 |
| distractor_robustness | core | push_breadcrumb_pull | 4 | 3 | 38472 | 0.0 | 12824.0 | 2320 |
| distractor_robustness | core | push_pages_upper_bound | 4 | 4 | 7146 | 0.0 | 1786.5 | 11450 |
| distractor_robustness | core | push_sections_pull | 4 | 4 | 47615 | 0.0 | 11903.8 | 10108 |
| follow_through | core | native_grep | 12 | 11 | 94440 | 0.0 | 8585.5 | n/a |
| follow_through | core | native_index | 12 | 12 | 451253 | 0.0 | 37604.4 | n/a |
| follow_through | core | none | 12 | 0 | 4943 | 0.0 | undefined | n/a |
| follow_through | core | oracle | 12 | 11 | 10642 | 0.0 | 967.5 | 12546 |
| follow_through | core | pull | 12 | 12 | 149965 | 0.0 | 12497.1 | n/a |
| follow_through | core | push_breadcrumb | 12 | 0 | 10370 | 0.0 | undefined | 8008 |
| follow_through | core | push_breadcrumb_pull | 12 | 9 | 211930 | 0.0 | 23547.8 | 8008 |
| follow_through | core | push_pages_upper_bound | 12 | 0 | 21157 | 0.0 | undefined | 35352 |
| follow_through | core | push_sections_pull | 12 | 12 | 110278 | 0.0 | 9189.8 | 32210 |
| multi_hop | core | native_grep | 3 | 0 | 26350 | 0.0 | undefined | n/a |
| multi_hop | core | native_index | 6 | 2 | 247853 | 0.0 | 123926.5 | n/a |
| multi_hop | core | none | 6 | 0 | 2559 | 0.0 | undefined | n/a |
| multi_hop | core | oracle | 6 | 5 | 6191 | 0.0 | 1238.2 | 8158 |
| multi_hop | core | pull | 6 | 2 | 115674 | 0.0 | 57837.0 | n/a |
| multi_hop | core | push_breadcrumb | 6 | 0 | 4030 | 0.0 | undefined | 3492 |
| multi_hop | core | push_breadcrumb_pull | 5 | 1 | 97319 | 0.0 | 97319.0 | 2932 |
| multi_hop | core | push_pages_upper_bound | 6 | 0 | 10211 | 0.0 | undefined | 16930 |
| multi_hop | core | push_sections_pull | 6 | 2 | 80907 | 0.0 | 40453.5 | 12878 |
| redundancy | core | native_grep | 8 | 4 | 34446 | 0.0 | 8611.5 | n/a |
| redundancy | core | native_index | 8 | 6 | 222584 | 0.0 | 37097.3 | n/a |
| redundancy | core | none | 8 | 0 | 2708 | 0.0 | undefined | n/a |
| redundancy | core | oracle | 8 | 6 | 7034 | 0.0 | 1172.3 | 10606 |
| redundancy | core | pull | 8 | 5 | 59453 | 0.0 | 11890.6 | n/a |
| redundancy | core | push_breadcrumb | 8 | 0 | 6232 | 0.0 | undefined | 5154 |
| redundancy | core | push_breadcrumb_pull | 8 | 4 | 75081 | 0.0 | 18770.2 | 5154 |
| redundancy | core | push_pages_upper_bound | 8 | 6 | 12391 | 0.0 | 2065.2 | 23706 |
| redundancy | core | push_sections_pull | 8 | 6 | 28574 | 0.0 | 4762.3 | 17154 |
| single_hop | core | native_grep | 15 | 15 | 83444 | 0.0 | 5562.9 | n/a |
| single_hop | core | native_index | 15 | 12 | 406519 | 0.0 | 33876.6 | n/a |
| single_hop | core | none | 15 | 0 | 5052 | 0.0 | undefined | n/a |
| single_hop | core | oracle | 15 | 14 | 14769 | 0.0 | 1054.9 | 19602 |
| single_hop | core | pull | 15 | 15 | 141246 | 0.0 | 9416.4 | n/a |
| single_hop | core | push_breadcrumb | 15 | 0 | 11624 | 0.0 | undefined | 9286 |
| single_hop | core | push_breadcrumb_pull | 15 | 15 | 153041 | 0.0 | 10202.7 | 9286 |
| single_hop | core | push_pages_upper_bound | 15 | 8 | 23504 | 0.0 | 2938.0 | 40360 |
| single_hop | core | push_sections_pull | 15 | 15 | 59439 | 0.0 | 3962.6 | 42267 |
| temporal | core | native_grep | 12 | 12 | 53224 | 0.0 | 4435.3 | n/a |
| temporal | core | native_index | 12 | 10 | 351392 | 0.0 | 35139.2 | n/a |
| temporal | core | none | 12 | 0 | 3865 | 0.0 | undefined | n/a |
| temporal | core | oracle | 12 | 12 | 6810 | 0.0 | 567.5 | 6464 |
| temporal | core | pull | 12 | 11 | 116162 | 0.0 | 10560.2 | n/a |
| temporal | core | push_breadcrumb | 12 | 0 | 8325 | 0.0 | undefined | 7314 |
| temporal | core | push_breadcrumb_pull | 12 | 10 | 122990 | 0.0 | 12299.0 | 7314 |
| temporal | core | push_pages_upper_bound | 12 | 12 | 19028 | 0.0 | 1585.7 | 30978 |
| temporal | core | push_sections_pull | 12 | 12 | 51380 | 0.0 | 4281.7 | 17954 |
| unprompted_push | core | native_grep | 18 | 6 | 188782 | 0.0 | 31463.7 | n/a |
| unprompted_push | core | native_index | 20 | 8 | 917434 | 0.0 | 114679.2 | n/a |
| unprompted_push | core | none | 20 | 0 | 7898 | 0.0 | undefined | n/a |
| unprompted_push | core | oracle | 20 | 9 | 23650 | 0.0 | 2627.8 | 31554 |
| unprompted_push | core | pull | 20 | 5 | 249538 | 0.0 | 49907.6 | n/a |
| unprompted_push | core | push_breadcrumb | 20 | 0 | 29555 | 0.0 | undefined | 13756 |
| unprompted_push | core | push_breadcrumb_pull | 20 | 7 | 399716 | 0.0 | 57102.3 | 13756 |
| unprompted_push | core | push_pages_upper_bound | 20 | 0 | 43548 | 0.0 | undefined | 68226 |
| unprompted_push | core | push_sections_pull | 20 | 12 | 254546 | 0.0 | 21212.2 | 46319 |

## Query quality

`lexical_overlap(a, b) = |terms(a) ∩ terms(b)| / |terms(a) ∪ terms(b)|` (Jaccard) — `terms()` is lowercased alphanumeric tokens of length >= 3, minus a short stopword list; denominator is the union of both term sets, 0.0 when either side is empty. `self_query_overlap` is measured between the agent's own recall-tool query and the target (ground-truth) page text; `topic_query_overlap` is the SAME formula against the same target, but using the probe's original query text verbatim as a zero-cost baseline (no extraction step is run to produce it — it already exists).

| probe_class | corpus_scale | n | self_query_overlap | topic_query_overlap |
| --- | --- | --- | --- | --- |
| abstention | core | 6 | 0.000 | 0.000 |
| disambiguation | core | 7 | 0.030 | 0.027 |
| distractor_robustness | core | 4 | 0.022 | 0.030 |
| follow_through | core | 12 | 0.057 | 0.054 |
| multi_hop | core | 6 | 0.046 | 0.047 |
| redundancy | core | 8 | 0.045 | 0.051 |
| single_hop | core | 15 | 0.050 | 0.059 |
| temporal | core | 12 | 0.063 | 0.068 |
| unprompted_push | core | 20 | 0.054 | 0.131 |

## Cost (per turn, not per task)

`mean_injected_context_tokens` (PUSH/ORACLE) is counted whether the delivered pages were used or not — that is the asymmetry against PULL, which pays only on a turn where it actually calls recall. Read the two columns side by side, never in isolation.

| probe_class | corpus_scale | arm | n | mean_input_tokens/turn | mean_output_tokens/turn | mean_injected_context_tokens |
| --- | --- | --- | --- | --- | --- | --- |
| abstention | core | native_grep | 6 | 1221.4 | 93.9 | n/a |
| abstention | core | native_index | 6 | 7896.3 | 90.2 | n/a |
| abstention | core | none | 6 | 280.7 | 47.5 | n/a |
| abstention | core | oracle | 6 | 280.7 | 46.0 | 0.0 |
| abstention | core | pull | 6 | 4851.9 | 125.1 | n/a |
| abstention | core | push_breadcrumb | 6 | 519.0 | 157.7 | 143.3 |
| abstention | core | push_breadcrumb_pull | 6 | 4413.9 | 134.0 | 143.3 |
| abstention | core | push_pages_upper_bound | 6 | 1489.7 | 71.2 | 731.0 |
| abstention | core | push_sections_pull | 6 | 4042.7 | 98.7 | 491.3 |
| disambiguation | core | native_grep | 7 | 2334.3 | 179.8 | n/a |
| disambiguation | core | native_index | 7 | 8966.2 | 175.3 | n/a |
| disambiguation | core | none | 7 | 278.6 | 51.9 | n/a |
| disambiguation | core | oracle | 7 | 953.0 | 222.7 | 401.1 |
| disambiguation | core | pull | 7 | 3495.1 | 203.0 | n/a |
| disambiguation | core | push_breadcrumb | 7 | 520.9 | 238.6 | 137.0 |
| disambiguation | core | push_breadcrumb_pull | 7 | 4617.8 | 226.0 | 137.0 |
| disambiguation | core | push_pages_upper_bound | 7 | 1529.9 | 260.0 | 744.6 |
| disambiguation | core | push_sections_pull | 7 | 3867.9 | 282.1 | 592.1 |
| distractor_robustness | core | native_grep | 4 | 2473.2 | 303.3 | n/a |
| distractor_robustness | core | native_index | 4 | 9107.6 | 311.1 | n/a |
| distractor_robustness | core | none | 4 | 273.5 | 71.2 | n/a |
| distractor_robustness | core | oracle | 4 | 790.0 | 124.8 | 296.5 |
| distractor_robustness | core | pull | 4 | 4894.2 | 210.4 | n/a |
| distractor_robustness | core | push_breadcrumb | 4 | 519.5 | 348.5 | 144.0 |
| distractor_robustness | core | push_breadcrumb_pull | 4 | 4520.8 | 288.2 | 144.0 |
| distractor_robustness | core | push_pages_upper_bound | 4 | 1442.5 | 344.0 | 711.5 |
| distractor_robustness | core | push_sections_pull | 4 | 5720.5 | 231.4 | 631.5 |
| follow_through | core | native_grep | 12 | 1502.4 | 98.3 | n/a |
| follow_through | core | native_index | 12 | 8546.9 | 131.0 | n/a |
| follow_through | core | none | 12 | 303.5 | 108.4 | n/a |
| follow_through | core | oracle | 12 | 734.8 | 152.0 | 261.3 |
| follow_through | core | pull | 12 | 4024.9 | 140.8 | n/a |
| follow_through | core | push_breadcrumb | 12 | 564.7 | 299.5 | 165.8 |
| follow_through | core | push_breadcrumb_pull | 12 | 5394.3 | 182.8 | 165.8 |
| follow_through | core | push_pages_upper_bound | 12 | 1486.3 | 276.8 | 733.0 |
| follow_through | core | push_sections_pull | 12 | 4408.5 | 186.4 | 670.5 |
| multi_hop | core | native_grep | 3 | 1912.1 | 114.8 | n/a |
| multi_hop | core | native_index | 6 | 9038.1 | 141.7 | n/a |
| multi_hop | core | none | 6 | 303.0 | 123.5 | n/a |
| multi_hop | core | oracle | 6 | 875.7 | 156.2 | 339.3 |
| multi_hop | core | pull | 6 | 5090.0 | 168.0 | n/a |
| multi_hop | core | push_breadcrumb | 6 | 538.3 | 133.3 | 144.7 |
| multi_hop | core | push_breadcrumb_pull | 5 | 5534.3 | 190.4 | 145.8 |
| multi_hop | core | push_pages_upper_bound | 6 | 1442.0 | 259.8 | 702.3 |
| multi_hop | core | push_sections_pull | 6 | 5197.2 | 196.6 | 536.3 |
| redundancy | core | native_grep | 8 | 1352.3 | 82.9 | n/a |
| redundancy | core | native_index | 8 | 8453.9 | 107.0 | n/a |
| redundancy | core | none | 8 | 281.8 | 56.8 | n/a |
| redundancy | core | oracle | 8 | 808.0 | 71.2 | 330.8 |
| redundancy | core | pull | 8 | 3395.4 | 101.8 | n/a |
| redundancy | core | push_breadcrumb | 8 | 544.5 | 234.5 | 160.2 |
| redundancy | core | push_breadcrumb_pull | 8 | 4043.7 | 127.4 | 160.2 |
| redundancy | core | push_pages_upper_bound | 8 | 1469.5 | 79.4 | 737.8 |
| redundancy | core | push_sections_pull | 8 | 3464.2 | 107.5 | 535.8 |
| single_hop | core | native_grep | 15 | 1637.0 | 138.4 | n/a |
| single_hop | core | native_index | 15 | 8335.6 | 133.6 | n/a |
| single_hop | core | none | 15 | 281.5 | 55.3 | n/a |
| single_hop | core | oracle | 15 | 742.1 | 242.5 | 326.3 |
| single_hop | core | pull | 15 | 3646.5 | 171.0 | n/a |
| single_hop | core | push_breadcrumb | 15 | 531.9 | 243.1 | 153.8 |
| single_hop | core | push_breadcrumb_pull | 15 | 3955.7 | 180.5 | 153.8 |
| single_hop | core | push_pages_upper_bound | 15 | 1388.3 | 178.7 | 669.3 |
| single_hop | core | push_sections_pull | 15 | 3652.2 | 310.4 | 704.1 |
| temporal | core | native_grep | 12 | 1397.6 | 80.9 | n/a |
| temporal | core | native_index | 12 | 8279.7 | 86.8 | n/a |
| temporal | core | none | 12 | 276.8 | 45.2 | n/a |
| temporal | core | oracle | 12 | 505.0 | 62.5 | 134.3 |
| temporal | core | pull | 12 | 3939.0 | 209.7 | n/a |
| temporal | core | push_breadcrumb | 12 | 524.5 | 169.2 | 151.7 |
| temporal | core | push_breadcrumb_pull | 12 | 4329.0 | 226.2 | 151.7 |
| temporal | core | push_pages_upper_bound | 12 | 1350.3 | 235.3 | 642.3 |
| temporal | core | push_sections_pull | 12 | 3394.1 | 275.9 | 373.5 |
| unprompted_push | core | native_grep | 18 | 1928.6 | 192.6 | n/a |
| unprompted_push | core | native_index | 20 | 8873.8 | 209.7 | n/a |
| unprompted_push | core | none | 20 | 310.9 | 84.0 | n/a |
| unprompted_push | core | oracle | 20 | 888.5 | 293.9 | 394.0 |
| unprompted_push | core | pull | 20 | 4317.8 | 303.3 | n/a |
| unprompted_push | core | push_breadcrumb | 20 | 574.2 | 903.5 | 171.0 |
| unprompted_push | core | push_breadcrumb_pull | 20 | 6086.3 | 360.8 | 171.0 |
| unprompted_push | core | push_pages_upper_bound | 20 | 1643.5 | 533.9 | 848.6 |
| unprompted_push | core | push_sections_pull | 20 | 4603.5 | 291.6 | 578.6 |

## Efficiency

| probe_class | corpus_scale | arm | n | mean_turn_count | mean_tool_call_count |
| --- | --- | --- | --- | --- | --- |
| abstention | core | native_grep | 6 | 3.83 | 4.17 |
| abstention | core | native_index | 6 | 2.00 | 1.00 |
| abstention | core | none | 6 | 1.00 | 0.00 |
| abstention | core | oracle | 6 | 1.00 | 0.00 |
| abstention | core | pull | 6 | 3.17 | 2.83 |
| abstention | core | push_breadcrumb | 6 | 1.00 | 0.00 |
| abstention | core | push_breadcrumb_pull | 6 | 2.67 | 2.17 |
| abstention | core | push_pages_upper_bound | 6 | 1.00 | 0.00 |
| abstention | core | push_sections_pull | 6 | 2.00 | 1.00 |
| disambiguation | core | native_grep | 7 | 3.29 | 3.43 |
| disambiguation | core | native_index | 7 | 3.71 | 5.71 |
| disambiguation | core | none | 7 | 1.00 | 0.00 |
| disambiguation | core | oracle | 7 | 1.00 | 0.00 |
| disambiguation | core | pull | 7 | 2.14 | 1.29 |
| disambiguation | core | push_breadcrumb | 7 | 1.00 | 0.00 |
| disambiguation | core | push_breadcrumb_pull | 7 | 2.43 | 2.00 |
| disambiguation | core | push_pages_upper_bound | 7 | 1.00 | 0.00 |
| disambiguation | core | push_sections_pull | 7 | 1.14 | 0.14 |
| distractor_robustness | core | native_grep | 4 | 3.50 | 10.75 |
| distractor_robustness | core | native_index | 4 | 3.25 | 5.75 |
| distractor_robustness | core | none | 4 | 1.00 | 0.00 |
| distractor_robustness | core | oracle | 4 | 1.00 | 0.00 |
| distractor_robustness | core | pull | 4 | 2.75 | 2.25 |
| distractor_robustness | core | push_breadcrumb | 4 | 1.00 | 0.00 |
| distractor_robustness | core | push_breadcrumb_pull | 4 | 2.00 | 1.50 |
| distractor_robustness | core | push_pages_upper_bound | 4 | 1.00 | 0.00 |
| distractor_robustness | core | push_sections_pull | 4 | 2.00 | 1.00 |
| follow_through | core | native_grep | 12 | 4.92 | 4.17 |
| follow_through | core | native_index | 12 | 4.33 | 5.92 |
| follow_through | core | none | 12 | 1.00 | 0.00 |
| follow_through | core | oracle | 12 | 1.00 | 0.00 |
| follow_through | core | pull | 12 | 3.00 | 2.42 |
| follow_through | core | push_breadcrumb | 12 | 1.00 | 0.00 |
| follow_through | core | push_breadcrumb_pull | 12 | 3.17 | 3.17 |
| follow_through | core | push_pages_upper_bound | 12 | 1.00 | 0.00 |
| follow_through | core | push_sections_pull | 12 | 2.00 | 1.33 |
| multi_hop | core | native_grep | 3 | 4.33 | 5.67 |
| multi_hop | core | native_index | 6 | 4.50 | 7.17 |
| multi_hop | core | none | 6 | 1.00 | 0.00 |
| multi_hop | core | oracle | 6 | 1.00 | 0.00 |
| multi_hop | core | pull | 6 | 3.67 | 4.33 |
| multi_hop | core | push_breadcrumb | 6 | 1.00 | 0.00 |
| multi_hop | core | push_breadcrumb_pull | 5 | 3.40 | 4.40 |
| multi_hop | core | push_pages_upper_bound | 6 | 1.00 | 0.00 |
| multi_hop | core | push_sections_pull | 6 | 2.50 | 2.67 |
| redundancy | core | native_grep | 8 | 3.00 | 2.50 |
| redundancy | core | native_index | 8 | 3.25 | 3.75 |
| redundancy | core | none | 8 | 1.00 | 0.00 |
| redundancy | core | oracle | 8 | 1.00 | 0.00 |
| redundancy | core | pull | 8 | 2.12 | 1.12 |
| redundancy | core | push_breadcrumb | 8 | 1.00 | 0.00 |
| redundancy | core | push_breadcrumb_pull | 8 | 2.25 | 1.25 |
| redundancy | core | push_pages_upper_bound | 8 | 1.00 | 0.00 |
| redundancy | core | push_sections_pull | 8 | 1.00 | 0.00 |
| single_hop | core | native_grep | 15 | 3.13 | 2.60 |
| single_hop | core | native_index | 15 | 3.20 | 2.27 |
| single_hop | core | none | 15 | 1.00 | 0.00 |
| single_hop | core | oracle | 15 | 1.00 | 0.00 |
| single_hop | core | pull | 15 | 2.47 | 1.47 |
| single_hop | core | push_breadcrumb | 15 | 1.00 | 0.00 |
| single_hop | core | push_breadcrumb_pull | 15 | 2.47 | 1.60 |
| single_hop | core | push_pages_upper_bound | 15 | 1.00 | 0.00 |
| single_hop | core | push_sections_pull | 15 | 1.00 | 0.00 |
| temporal | core | native_grep | 12 | 3.00 | 2.50 |
| temporal | core | native_index | 12 | 3.50 | 3.42 |
| temporal | core | none | 12 | 1.00 | 0.00 |
| temporal | core | oracle | 12 | 1.00 | 0.00 |
| temporal | core | pull | 12 | 2.33 | 1.67 |
| temporal | core | push_breadcrumb | 12 | 1.00 | 0.00 |
| temporal | core | push_breadcrumb_pull | 12 | 2.25 | 1.92 |
| temporal | core | push_pages_upper_bound | 12 | 1.00 | 0.00 |
| temporal | core | push_sections_pull | 12 | 1.17 | 0.17 |
| unprompted_push | core | native_grep | 18 | 4.94 | 6.17 |
| unprompted_push | core | native_index | 20 | 5.05 | 7.65 |
| unprompted_push | core | none | 20 | 1.00 | 0.00 |
| unprompted_push | core | oracle | 20 | 1.00 | 0.00 |
| unprompted_push | core | pull | 20 | 2.70 | 2.50 |
| unprompted_push | core | push_breadcrumb | 20 | 1.00 | 0.00 |
| unprompted_push | core | push_breadcrumb_pull | 20 | 3.10 | 4.15 |
| unprompted_push | core | push_pages_upper_bound | 20 | 1.00 | 0.00 |
| unprompted_push | core | push_sections_pull | 20 | 2.60 | 1.60 |

## Waste

`wasted_page_fraction = 1 - uid_citation_rate` — the fraction of delivered pages whose uid never appears in the answer text, i.e. never referenced anywhere later in the rollout. `wasted_tokens_estimate` distributes the delivered content's estimated token count evenly across delivered uids and sums the wasted share. `n/a` means nothing was delivered for that group (no basis to call anything wasted).

| probe_class | corpus_scale | arm | n | wasted_page_fraction | wasted_tokens_estimate |
| --- | --- | --- | --- | --- | --- |
| abstention | core | native_grep | 6 | n/a | n/a |
| abstention | core | native_index | 6 | n/a | n/a |
| abstention | core | none | 6 | n/a | n/a |
| abstention | core | oracle | 6 | n/a | n/a |
| abstention | core | pull | 6 | 1.000 | 2291.7 |
| abstention | core | push_breadcrumb | 6 | n/a | n/a |
| abstention | core | push_breadcrumb_pull | 6 | 1.000 | 1899.5 |
| abstention | core | push_pages_upper_bound | 6 | 1.000 | 731.0 |
| abstention | core | push_sections_pull | 6 | 1.000 | 1234.7 |
| disambiguation | core | native_grep | 7 | n/a | n/a |
| disambiguation | core | native_index | 7 | n/a | n/a |
| disambiguation | core | none | 7 | n/a | n/a |
| disambiguation | core | oracle | 7 | 1.000 | 401.1 |
| disambiguation | core | pull | 7 | 0.914 | 803.6 |
| disambiguation | core | push_breadcrumb | 7 | n/a | n/a |
| disambiguation | core | push_breadcrumb_pull | 7 | 0.886 | 1566.9 |
| disambiguation | core | push_pages_upper_bound | 7 | 0.971 | 723.5 |
| disambiguation | core | push_sections_pull | 7 | 1.000 | 771.3 |
| distractor_robustness | core | native_grep | 4 | n/a | n/a |
| distractor_robustness | core | native_index | 4 | n/a | n/a |
| distractor_robustness | core | none | 4 | n/a | n/a |
| distractor_robustness | core | oracle | 4 | 1.000 | 296.5 |
| distractor_robustness | core | pull | 4 | 1.000 | 2496.8 |
| distractor_robustness | core | push_breadcrumb | 4 | n/a | n/a |
| distractor_robustness | core | push_breadcrumb_pull | 4 | 1.000 | 1988.5 |
| distractor_robustness | core | push_pages_upper_bound | 4 | 1.000 | 711.5 |
| distractor_robustness | core | push_sections_pull | 4 | 1.000 | 2344.0 |
| follow_through | core | native_grep | 12 | n/a | n/a |
| follow_through | core | native_index | 12 | n/a | n/a |
| follow_through | core | none | 12 | n/a | n/a |
| follow_through | core | oracle | 12 | 1.000 | 261.3 |
| follow_through | core | pull | 12 | 1.000 | 1406.3 |
| follow_through | core | push_breadcrumb | 12 | n/a | n/a |
| follow_through | core | push_breadcrumb_pull | 12 | 1.000 | 2692.4 |
| follow_through | core | push_pages_upper_bound | 12 | 1.000 | 733.0 |
| follow_through | core | push_sections_pull | 12 | 1.000 | 1540.9 |
| multi_hop | core | native_grep | 3 | n/a | n/a |
| multi_hop | core | native_index | 6 | n/a | n/a |
| multi_hop | core | none | 6 | n/a | n/a |
| multi_hop | core | oracle | 6 | 1.000 | 339.3 |
| multi_hop | core | pull | 6 | 1.000 | 2775.2 |
| multi_hop | core | push_breadcrumb | 6 | n/a | n/a |
| multi_hop | core | push_breadcrumb_pull | 5 | 1.000 | 2959.6 |
| multi_hop | core | push_pages_upper_bound | 6 | 1.000 | 702.3 |
| multi_hop | core | push_sections_pull | 6 | 1.000 | 2264.0 |
| redundancy | core | native_grep | 8 | n/a | n/a |
| redundancy | core | native_index | 8 | n/a | n/a |
| redundancy | core | none | 8 | n/a | n/a |
| redundancy | core | oracle | 8 | 1.000 | 330.8 |
| redundancy | core | pull | 8 | 0.975 | 775.9 |
| redundancy | core | push_breadcrumb | 8 | n/a | n/a |
| redundancy | core | push_breadcrumb_pull | 8 | 1.000 | 1272.2 |
| redundancy | core | push_pages_upper_bound | 8 | 1.000 | 737.8 |
| redundancy | core | push_sections_pull | 8 | 1.000 | 535.8 |
| single_hop | core | native_grep | 15 | n/a | n/a |
| single_hop | core | native_index | 15 | n/a | n/a |
| single_hop | core | none | 15 | n/a | n/a |
| single_hop | core | oracle | 15 | 1.000 | 326.3 |
| single_hop | core | pull | 15 | 1.000 | 1064.9 |
| single_hop | core | push_breadcrumb | 15 | n/a | n/a |
| single_hop | core | push_breadcrumb_pull | 15 | 1.000 | 1297.2 |
| single_hop | core | push_pages_upper_bound | 15 | 1.000 | 669.3 |
| single_hop | core | push_sections_pull | 15 | 1.000 | 704.1 |
| temporal | core | native_grep | 12 | n/a | n/a |
| temporal | core | native_index | 12 | n/a | n/a |
| temporal | core | none | 12 | n/a | n/a |
| temporal | core | oracle | 12 | 1.000 | 134.3 |
| temporal | core | pull | 12 | 1.000 | 1074.4 |
| temporal | core | push_breadcrumb | 12 | n/a | n/a |
| temporal | core | push_breadcrumb_pull | 12 | 1.000 | 1426.3 |
| temporal | core | push_pages_upper_bound | 12 | 1.000 | 642.3 |
| temporal | core | push_sections_pull | 12 | 1.000 | 467.8 |
| unprompted_push | core | native_grep | 18 | n/a | n/a |
| unprompted_push | core | native_index | 20 | n/a | n/a |
| unprompted_push | core | none | 20 | n/a | n/a |
| unprompted_push | core | oracle | 20 | 1.000 | 394.0 |
| unprompted_push | core | pull | 20 | 0.933 | 1811.8 |
| unprompted_push | core | push_breadcrumb | 20 | n/a | n/a |
| unprompted_push | core | push_breadcrumb_pull | 20 | 0.971 | 3270.2 |
| unprompted_push | core | push_pages_upper_bound | 20 | 0.750 | 638.4 |
| unprompted_push | core | push_sections_pull | 20 | 0.869 | 1485.5 |

## Utilization (free portion only)

`uid_citation_rate` = fraction of delivered uids whose literal uid string appears in the answer text. `distinctive_ngram_overlap` = `|ngrams(delivered) ∩ ngrams(answer)| / |ngrams(delivered)|` over 4-word shingles — denominator is the delivered text's own shingle count. Both are coarse, zero-judgment proxies for "did the answer draw on what was delivered" — the judged per-claim support check is explicitly out of scope for this report.

| probe_class | corpus_scale | arm | n | uid_citation_rate | distinctive_ngram_overlap |
| --- | --- | --- | --- | --- | --- |
| abstention | core | native_grep | 6 | n/a | n/a |
| abstention | core | native_index | 6 | n/a | n/a |
| abstention | core | none | 6 | n/a | n/a |
| abstention | core | oracle | 6 | n/a | n/a |
| abstention | core | pull | 6 | 0.000 | 0.000 |
| abstention | core | push_breadcrumb | 6 | n/a | 0.069 |
| abstention | core | push_breadcrumb_pull | 6 | 0.000 | 0.002 |
| abstention | core | push_pages_upper_bound | 6 | 0.000 | 0.000 |
| abstention | core | push_sections_pull | 6 | 0.000 | 0.000 |
| disambiguation | core | native_grep | 7 | n/a | n/a |
| disambiguation | core | native_index | 7 | n/a | n/a |
| disambiguation | core | none | 7 | n/a | n/a |
| disambiguation | core | oracle | 7 | 0.000 | 0.110 |
| disambiguation | core | pull | 7 | 0.086 | 0.057 |
| disambiguation | core | push_breadcrumb | 7 | n/a | 0.038 |
| disambiguation | core | push_breadcrumb_pull | 7 | 0.114 | 0.028 |
| disambiguation | core | push_pages_upper_bound | 7 | 0.029 | 0.084 |
| disambiguation | core | push_sections_pull | 7 | 0.000 | 0.055 |
| distractor_robustness | core | native_grep | 4 | n/a | n/a |
| distractor_robustness | core | native_index | 4 | n/a | n/a |
| distractor_robustness | core | none | 4 | n/a | n/a |
| distractor_robustness | core | oracle | 4 | 0.000 | 0.144 |
| distractor_robustness | core | pull | 4 | 0.000 | 0.028 |
| distractor_robustness | core | push_breadcrumb | 4 | n/a | 0.112 |
| distractor_robustness | core | push_breadcrumb_pull | 4 | 0.000 | 0.014 |
| distractor_robustness | core | push_pages_upper_bound | 4 | 0.000 | 0.076 |
| distractor_robustness | core | push_sections_pull | 4 | 0.000 | 0.021 |
| follow_through | core | native_grep | 12 | n/a | n/a |
| follow_through | core | native_index | 12 | n/a | n/a |
| follow_through | core | none | 12 | n/a | n/a |
| follow_through | core | oracle | 12 | 0.000 | 0.220 |
| follow_through | core | pull | 12 | 0.000 | 0.036 |
| follow_through | core | push_breadcrumb | 12 | n/a | 0.059 |
| follow_through | core | push_breadcrumb_pull | 12 | 0.000 | 0.023 |
| follow_through | core | push_pages_upper_bound | 12 | 0.000 | 0.032 |
| follow_through | core | push_sections_pull | 12 | 0.000 | 0.034 |
| multi_hop | core | native_grep | 3 | n/a | n/a |
| multi_hop | core | native_index | 6 | n/a | n/a |
| multi_hop | core | none | 6 | n/a | n/a |
| multi_hop | core | oracle | 6 | 0.000 | 0.081 |
| multi_hop | core | pull | 6 | 0.000 | 0.010 |
| multi_hop | core | push_breadcrumb | 6 | n/a | 0.012 |
| multi_hop | core | push_breadcrumb_pull | 5 | 0.000 | 0.009 |
| multi_hop | core | push_pages_upper_bound | 6 | 0.000 | 0.014 |
| multi_hop | core | push_sections_pull | 6 | 0.000 | 0.010 |
| redundancy | core | native_grep | 8 | n/a | n/a |
| redundancy | core | native_index | 8 | n/a | n/a |
| redundancy | core | none | 8 | n/a | n/a |
| redundancy | core | oracle | 8 | 0.000 | 0.103 |
| redundancy | core | pull | 8 | 0.025 | 0.053 |
| redundancy | core | push_breadcrumb | 8 | n/a | 0.032 |
| redundancy | core | push_breadcrumb_pull | 8 | 0.000 | 0.035 |
| redundancy | core | push_pages_upper_bound | 8 | 0.000 | 0.047 |
| redundancy | core | push_sections_pull | 8 | 0.000 | 0.075 |
| single_hop | core | native_grep | 15 | n/a | n/a |
| single_hop | core | native_index | 15 | n/a | n/a |
| single_hop | core | none | 15 | n/a | n/a |
| single_hop | core | oracle | 15 | 0.000 | 0.272 |
| single_hop | core | pull | 15 | 0.000 | 0.072 |
| single_hop | core | push_breadcrumb | 15 | n/a | 0.026 |
| single_hop | core | push_breadcrumb_pull | 15 | 0.000 | 0.065 |
| single_hop | core | push_pages_upper_bound | 15 | 0.000 | 0.060 |
| single_hop | core | push_sections_pull | 15 | 0.000 | 0.105 |
| temporal | core | native_grep | 12 | n/a | n/a |
| temporal | core | native_index | 12 | n/a | n/a |
| temporal | core | none | 12 | n/a | n/a |
| temporal | core | oracle | 12 | 0.000 | 0.112 |
| temporal | core | pull | 12 | 0.000 | 0.026 |
| temporal | core | push_breadcrumb | 12 | n/a | 0.028 |
| temporal | core | push_breadcrumb_pull | 12 | 0.000 | 0.021 |
| temporal | core | push_pages_upper_bound | 12 | 0.000 | 0.040 |
| temporal | core | push_sections_pull | 12 | 0.000 | 0.060 |
| unprompted_push | core | native_grep | 18 | n/a | n/a |
| unprompted_push | core | native_index | 20 | n/a | n/a |
| unprompted_push | core | none | 20 | n/a | n/a |
| unprompted_push | core | oracle | 20 | 0.000 | 0.169 |
| unprompted_push | core | pull | 20 | 0.067 | 0.045 |
| unprompted_push | core | push_breadcrumb | 20 | n/a | 0.107 |
| unprompted_push | core | push_breadcrumb_pull | 20 | 0.029 | 0.033 |
| unprompted_push | core | push_pages_upper_bound | 20 | 0.250 | 0.046 |
| unprompted_push | core | push_sections_pull | 20 | 0.131 | 0.052 |

## Correctness (answer ground truth, issue athenaeum#1573)

`correctness_rate` grades the ANSWER, not retrieval: normalized substring match against each probe's planted `answer_tokens` (non-abstention), or the declining-language rule for abstention probes — see `grade_correctness`. No LLM judge. This is what makes NONE (floor) and ORACLE (ceiling) readable as numbers for the first time — every other dimension above describes retrieval or cost, never whether the final answer was actually right. `n/a` means no probe in that group carries ground truth tokens to grade against. Those tokens are the corpus pages' own internal reference tags, so every arm's system prompt carries one identical instruction (issue athenaeum#1753) to end the answer with `[ref: TAG]` for each page relied on, or `[ref: none]` for none — correctness therefore reads as “did the arm reach the right page and say so”. Rows recorded before that contract landed carry no tags and grade at or near 0 for every arm, including ORACLE.

| probe_class | corpus_scale | arm | n | correctness_rate |
| --- | --- | --- | --- | --- |
| abstention | core | native_grep | 6 | 1.000 |
| abstention | core | native_index | 6 | 0.333 |
| abstention | core | none | 6 | 1.000 |
| abstention | core | oracle | 6 | 1.000 |
| abstention | core | pull | 6 | 1.000 |
| abstention | core | push_breadcrumb | 6 | 1.000 |
| abstention | core | push_breadcrumb_pull | 6 | 0.500 |
| abstention | core | push_pages_upper_bound | 6 | 1.000 |
| abstention | core | push_sections_pull | 6 | 0.833 |
| disambiguation | core | native_grep | 7 | 0.571 |
| disambiguation | core | native_index | 7 | 1.000 |
| disambiguation | core | none | 7 | 0.000 |
| disambiguation | core | oracle | 7 | 1.000 |
| disambiguation | core | pull | 7 | 1.000 |
| disambiguation | core | push_breadcrumb | 7 | 0.000 |
| disambiguation | core | push_breadcrumb_pull | 7 | 1.000 |
| disambiguation | core | push_pages_upper_bound | 7 | 1.000 |
| disambiguation | core | push_sections_pull | 7 | 1.000 |
| distractor_robustness | core | native_grep | 4 | 0.750 |
| distractor_robustness | core | native_index | 4 | 0.750 |
| distractor_robustness | core | none | 4 | 0.000 |
| distractor_robustness | core | oracle | 4 | 1.000 |
| distractor_robustness | core | pull | 4 | 1.000 |
| distractor_robustness | core | push_breadcrumb | 4 | 0.000 |
| distractor_robustness | core | push_breadcrumb_pull | 4 | 0.750 |
| distractor_robustness | core | push_pages_upper_bound | 4 | 1.000 |
| distractor_robustness | core | push_sections_pull | 4 | 1.000 |
| follow_through | core | native_grep | 12 | 0.917 |
| follow_through | core | native_index | 12 | 1.000 |
| follow_through | core | none | 12 | 0.000 |
| follow_through | core | oracle | 12 | 0.917 |
| follow_through | core | pull | 12 | 1.000 |
| follow_through | core | push_breadcrumb | 12 | 0.000 |
| follow_through | core | push_breadcrumb_pull | 12 | 0.750 |
| follow_through | core | push_pages_upper_bound | 12 | 0.000 |
| follow_through | core | push_sections_pull | 12 | 1.000 |
| multi_hop | core | native_grep | 3 | 0.000 |
| multi_hop | core | native_index | 6 | 0.333 |
| multi_hop | core | none | 6 | 0.000 |
| multi_hop | core | oracle | 6 | 0.833 |
| multi_hop | core | pull | 6 | 0.333 |
| multi_hop | core | push_breadcrumb | 6 | 0.000 |
| multi_hop | core | push_breadcrumb_pull | 5 | 0.200 |
| multi_hop | core | push_pages_upper_bound | 6 | 0.000 |
| multi_hop | core | push_sections_pull | 6 | 0.333 |
| redundancy | core | native_grep | 8 | 0.500 |
| redundancy | core | native_index | 8 | 0.750 |
| redundancy | core | none | 8 | 0.000 |
| redundancy | core | oracle | 8 | 0.750 |
| redundancy | core | pull | 8 | 0.625 |
| redundancy | core | push_breadcrumb | 8 | 0.000 |
| redundancy | core | push_breadcrumb_pull | 8 | 0.500 |
| redundancy | core | push_pages_upper_bound | 8 | 0.750 |
| redundancy | core | push_sections_pull | 8 | 0.750 |
| single_hop | core | native_grep | 15 | 1.000 |
| single_hop | core | native_index | 15 | 0.800 |
| single_hop | core | none | 15 | 0.000 |
| single_hop | core | oracle | 15 | 0.933 |
| single_hop | core | pull | 15 | 1.000 |
| single_hop | core | push_breadcrumb | 15 | 0.000 |
| single_hop | core | push_breadcrumb_pull | 15 | 1.000 |
| single_hop | core | push_pages_upper_bound | 15 | 0.533 |
| single_hop | core | push_sections_pull | 15 | 1.000 |
| temporal | core | native_grep | 12 | 1.000 |
| temporal | core | native_index | 12 | 0.833 |
| temporal | core | none | 12 | 0.000 |
| temporal | core | oracle | 12 | 1.000 |
| temporal | core | pull | 12 | 0.917 |
| temporal | core | push_breadcrumb | 12 | 0.000 |
| temporal | core | push_breadcrumb_pull | 12 | 0.833 |
| temporal | core | push_pages_upper_bound | 12 | 1.000 |
| temporal | core | push_sections_pull | 12 | 1.000 |
| unprompted_push | core | native_grep | 18 | 0.333 |
| unprompted_push | core | native_index | 20 | 0.400 |
| unprompted_push | core | none | 20 | 0.000 |
| unprompted_push | core | oracle | 20 | 0.450 |
| unprompted_push | core | pull | 20 | 0.250 |
| unprompted_push | core | push_breadcrumb | 20 | 0.000 |
| unprompted_push | core | push_breadcrumb_pull | 20 | 0.350 |
| unprompted_push | core | push_pages_upper_bound | 20 | 0.000 |
| unprompted_push | core | push_sections_pull | 20 | 0.600 |

## Harm (forbidden-token) rate (issue athenaeum#1772, report_only)

`harm_free_rate` grades whether the answer avoided every one of a probe's planted `forbidden_tokens` -- see `grade_harm`. Same normalizer and substring instrument as `correctness_rate`, no LLM judge. `report_only` (issue athenaeum#1791 §2.1): this mechanism feeds no §7 condition and `compute_verdicts` is unchanged by it. `n/a` means no probe in that group carries `forbidden_tokens` to grade against -- true for every probe class shipped so far, so this section reads `n/a` throughout the current corpus.

| probe_class | corpus_scale | arm | n | harm_free_rate |
| --- | --- | --- | --- | --- |
| abstention | core | native_grep | 6 | n/a |
| abstention | core | native_index | 6 | n/a |
| abstention | core | none | 6 | n/a |
| abstention | core | oracle | 6 | n/a |
| abstention | core | pull | 6 | n/a |
| abstention | core | push_breadcrumb | 6 | n/a |
| abstention | core | push_breadcrumb_pull | 6 | n/a |
| abstention | core | push_pages_upper_bound | 6 | n/a |
| abstention | core | push_sections_pull | 6 | n/a |
| disambiguation | core | native_grep | 7 | n/a |
| disambiguation | core | native_index | 7 | n/a |
| disambiguation | core | none | 7 | n/a |
| disambiguation | core | oracle | 7 | n/a |
| disambiguation | core | pull | 7 | n/a |
| disambiguation | core | push_breadcrumb | 7 | n/a |
| disambiguation | core | push_breadcrumb_pull | 7 | n/a |
| disambiguation | core | push_pages_upper_bound | 7 | n/a |
| disambiguation | core | push_sections_pull | 7 | n/a |
| distractor_robustness | core | native_grep | 4 | n/a |
| distractor_robustness | core | native_index | 4 | n/a |
| distractor_robustness | core | none | 4 | n/a |
| distractor_robustness | core | oracle | 4 | n/a |
| distractor_robustness | core | pull | 4 | n/a |
| distractor_robustness | core | push_breadcrumb | 4 | n/a |
| distractor_robustness | core | push_breadcrumb_pull | 4 | n/a |
| distractor_robustness | core | push_pages_upper_bound | 4 | n/a |
| distractor_robustness | core | push_sections_pull | 4 | n/a |
| follow_through | core | native_grep | 12 | n/a |
| follow_through | core | native_index | 12 | n/a |
| follow_through | core | none | 12 | n/a |
| follow_through | core | oracle | 12 | n/a |
| follow_through | core | pull | 12 | n/a |
| follow_through | core | push_breadcrumb | 12 | n/a |
| follow_through | core | push_breadcrumb_pull | 12 | n/a |
| follow_through | core | push_pages_upper_bound | 12 | n/a |
| follow_through | core | push_sections_pull | 12 | n/a |
| multi_hop | core | native_grep | 3 | n/a |
| multi_hop | core | native_index | 6 | n/a |
| multi_hop | core | none | 6 | n/a |
| multi_hop | core | oracle | 6 | n/a |
| multi_hop | core | pull | 6 | n/a |
| multi_hop | core | push_breadcrumb | 6 | n/a |
| multi_hop | core | push_breadcrumb_pull | 5 | n/a |
| multi_hop | core | push_pages_upper_bound | 6 | n/a |
| multi_hop | core | push_sections_pull | 6 | n/a |
| redundancy | core | native_grep | 8 | n/a |
| redundancy | core | native_index | 8 | n/a |
| redundancy | core | none | 8 | n/a |
| redundancy | core | oracle | 8 | n/a |
| redundancy | core | pull | 8 | n/a |
| redundancy | core | push_breadcrumb | 8 | n/a |
| redundancy | core | push_breadcrumb_pull | 8 | n/a |
| redundancy | core | push_pages_upper_bound | 8 | n/a |
| redundancy | core | push_sections_pull | 8 | n/a |
| single_hop | core | native_grep | 15 | n/a |
| single_hop | core | native_index | 15 | n/a |
| single_hop | core | none | 15 | n/a |
| single_hop | core | oracle | 15 | n/a |
| single_hop | core | pull | 15 | n/a |
| single_hop | core | push_breadcrumb | 15 | n/a |
| single_hop | core | push_breadcrumb_pull | 15 | n/a |
| single_hop | core | push_pages_upper_bound | 15 | n/a |
| single_hop | core | push_sections_pull | 15 | n/a |
| temporal | core | native_grep | 12 | n/a |
| temporal | core | native_index | 12 | n/a |
| temporal | core | none | 12 | n/a |
| temporal | core | oracle | 12 | n/a |
| temporal | core | pull | 12 | n/a |
| temporal | core | push_breadcrumb | 12 | n/a |
| temporal | core | push_breadcrumb_pull | 12 | n/a |
| temporal | core | push_pages_upper_bound | 12 | n/a |
| temporal | core | push_sections_pull | 12 | n/a |
| unprompted_push | core | native_grep | 18 | 0.556 |
| unprompted_push | core | native_index | 20 | 0.400 |
| unprompted_push | core | none | 20 | 1.000 |
| unprompted_push | core | oracle | 20 | 1.000 |
| unprompted_push | core | pull | 20 | 0.450 |
| unprompted_push | core | push_breadcrumb | 20 | 1.000 |
| unprompted_push | core | push_breadcrumb_pull | 20 | 0.300 |
| unprompted_push | core | push_pages_upper_bound | 20 | 0.500 |
| unprompted_push | core | push_sections_pull | 20 | 0.600 |

## Coverage (fraction of planted tokens) (issue athenaeum#1773, report_only)

`coverage_rate` is the mean fraction of a probe's `answer_tokens` present in the answer -- see `grade_coverage`. Same normalizer and substring instrument as `correctness_rate`, but a ratio rather than an all-or-nothing match, so a many-correct-answer probe (the `aggregation` class) can grade partial credit instead of a forced zero. `report_only` (issue athenaeum#1791 §2.1): this mechanism feeds no §7 condition and `compute_verdicts` is unchanged by it. `n/a` means no probe in that group carries `answer_tokens` to grade against. For a single-`answer_tokens` probe, `coverage_rate` and `correctness_rate` are the same number by construction.

| probe_class | corpus_scale | arm | n | coverage_rate |
| --- | --- | --- | --- | --- |
| abstention | core | native_grep | 6 | n/a |
| abstention | core | native_index | 6 | n/a |
| abstention | core | none | 6 | n/a |
| abstention | core | oracle | 6 | n/a |
| abstention | core | pull | 6 | n/a |
| abstention | core | push_breadcrumb | 6 | n/a |
| abstention | core | push_breadcrumb_pull | 6 | n/a |
| abstention | core | push_pages_upper_bound | 6 | n/a |
| abstention | core | push_sections_pull | 6 | n/a |
| disambiguation | core | native_grep | 7 | 0.571 |
| disambiguation | core | native_index | 7 | 1.000 |
| disambiguation | core | none | 7 | 0.000 |
| disambiguation | core | oracle | 7 | 1.000 |
| disambiguation | core | pull | 7 | 0.286 |
| disambiguation | core | push_breadcrumb | 7 | 0.000 |
| disambiguation | core | push_breadcrumb_pull | 7 | 0.714 |
| disambiguation | core | push_pages_upper_bound | 7 | 0.429 |
| disambiguation | core | push_sections_pull | 7 | 1.000 |
| distractor_robustness | core | native_grep | 4 | 1.000 |
| distractor_robustness | core | native_index | 4 | 0.750 |
| distractor_robustness | core | none | 4 | 0.000 |
| distractor_robustness | core | oracle | 4 | 1.000 |
| distractor_robustness | core | pull | 4 | 1.000 |
| distractor_robustness | core | push_breadcrumb | 4 | 0.000 |
| distractor_robustness | core | push_breadcrumb_pull | 4 | 1.000 |
| distractor_robustness | core | push_pages_upper_bound | 4 | 1.000 |
| distractor_robustness | core | push_sections_pull | 4 | 1.000 |
| follow_through | core | native_grep | 12 | 1.000 |
| follow_through | core | native_index | 12 | 1.000 |
| follow_through | core | none | 12 | 0.000 |
| follow_through | core | oracle | 12 | 1.000 |
| follow_through | core | pull | 12 | 1.000 |
| follow_through | core | push_breadcrumb | 12 | 0.000 |
| follow_through | core | push_breadcrumb_pull | 12 | 0.917 |
| follow_through | core | push_pages_upper_bound | 12 | 0.500 |
| follow_through | core | push_sections_pull | 12 | 1.000 |
| multi_hop | core | native_grep | 3 | 1.000 |
| multi_hop | core | native_index | 6 | 1.000 |
| multi_hop | core | none | 6 | 0.000 |
| multi_hop | core | oracle | 6 | 1.000 |
| multi_hop | core | pull | 6 | 1.000 |
| multi_hop | core | push_breadcrumb | 6 | 0.000 |
| multi_hop | core | push_breadcrumb_pull | 5 | 1.000 |
| multi_hop | core | push_pages_upper_bound | 6 | 0.000 |
| multi_hop | core | push_sections_pull | 6 | 1.000 |
| redundancy | core | native_grep | 8 | 0.812 |
| redundancy | core | native_index | 8 | 1.000 |
| redundancy | core | none | 8 | 0.000 |
| redundancy | core | oracle | 8 | 0.875 |
| redundancy | core | pull | 8 | 0.375 |
| redundancy | core | push_breadcrumb | 8 | 0.000 |
| redundancy | core | push_breadcrumb_pull | 8 | 0.500 |
| redundancy | core | push_pages_upper_bound | 8 | 0.312 |
| redundancy | core | push_sections_pull | 8 | 0.875 |
| single_hop | core | native_grep | 15 | 1.000 |
| single_hop | core | native_index | 15 | 0.800 |
| single_hop | core | none | 15 | 0.000 |
| single_hop | core | oracle | 15 | 1.000 |
| single_hop | core | pull | 15 | 1.000 |
| single_hop | core | push_breadcrumb | 15 | 0.000 |
| single_hop | core | push_breadcrumb_pull | 15 | 1.000 |
| single_hop | core | push_pages_upper_bound | 15 | 0.533 |
| single_hop | core | push_sections_pull | 15 | 1.000 |
| temporal | core | native_grep | 12 | 1.000 |
| temporal | core | native_index | 12 | 0.833 |
| temporal | core | none | 12 | 0.000 |
| temporal | core | oracle | 12 | 1.000 |
| temporal | core | pull | 12 | 0.917 |
| temporal | core | push_breadcrumb | 12 | 0.000 |
| temporal | core | push_breadcrumb_pull | 12 | 0.833 |
| temporal | core | push_pages_upper_bound | 12 | 1.000 |
| temporal | core | push_sections_pull | 12 | 1.000 |
| unprompted_push | core | native_grep | 18 | 1.000 |
| unprompted_push | core | native_index | 20 | 1.000 |
| unprompted_push | core | none | 20 | 0.000 |
| unprompted_push | core | oracle | 20 | 1.000 |
| unprompted_push | core | pull | 20 | 0.800 |
| unprompted_push | core | push_breadcrumb | 20 | 0.000 |
| unprompted_push | core | push_breadcrumb_pull | 20 | 0.800 |
| unprompted_push | core | push_pages_upper_bound | 20 | 0.000 |
| unprompted_push | core | push_sections_pull | 20 | 0.800 |

## Marker resolution (issue athenaeum#1730, report_only)

`marker_resolution_rate` is the share of answers whose CITED `[^src-N]` footnote markers all resolve to a footnote definition on a page that plants one of the probe's `answer_tokens` -- see `grade_marker_resolution`. It asks whether an answer that quotes a citation quoted a real one, now that compiled pages carry inline per-claim markers and `recall` resolves them on the hit. `report_only`: this mechanism feeds no §7 condition and `compute_verdicts` is unchanged by it. `n/a` means no answer in that group cited a marker, or no token-bearing expected page defines one -- both are 'nothing to grade', never a graded zero.

| probe_class | corpus_scale | arm | n | marker_resolution_rate |
| --- | --- | --- | --- | --- |
| abstention | core | native_grep | 6 | n/a |
| abstention | core | native_index | 6 | n/a |
| abstention | core | none | 6 | n/a |
| abstention | core | oracle | 6 | n/a |
| abstention | core | pull | 6 | n/a |
| abstention | core | push_breadcrumb | 6 | n/a |
| abstention | core | push_breadcrumb_pull | 6 | n/a |
| abstention | core | push_pages_upper_bound | 6 | n/a |
| abstention | core | push_sections_pull | 6 | n/a |
| disambiguation | core | native_grep | 7 | n/a |
| disambiguation | core | native_index | 7 | n/a |
| disambiguation | core | none | 7 | n/a |
| disambiguation | core | oracle | 7 | n/a |
| disambiguation | core | pull | 7 | n/a |
| disambiguation | core | push_breadcrumb | 7 | n/a |
| disambiguation | core | push_breadcrumb_pull | 7 | n/a |
| disambiguation | core | push_pages_upper_bound | 7 | n/a |
| disambiguation | core | push_sections_pull | 7 | n/a |
| distractor_robustness | core | native_grep | 4 | n/a |
| distractor_robustness | core | native_index | 4 | n/a |
| distractor_robustness | core | none | 4 | n/a |
| distractor_robustness | core | oracle | 4 | n/a |
| distractor_robustness | core | pull | 4 | n/a |
| distractor_robustness | core | push_breadcrumb | 4 | n/a |
| distractor_robustness | core | push_breadcrumb_pull | 4 | n/a |
| distractor_robustness | core | push_pages_upper_bound | 4 | n/a |
| distractor_robustness | core | push_sections_pull | 4 | n/a |
| follow_through | core | native_grep | 12 | n/a |
| follow_through | core | native_index | 12 | n/a |
| follow_through | core | none | 12 | n/a |
| follow_through | core | oracle | 12 | n/a |
| follow_through | core | pull | 12 | n/a |
| follow_through | core | push_breadcrumb | 12 | n/a |
| follow_through | core | push_breadcrumb_pull | 12 | n/a |
| follow_through | core | push_pages_upper_bound | 12 | n/a |
| follow_through | core | push_sections_pull | 12 | n/a |
| multi_hop | core | native_grep | 3 | n/a |
| multi_hop | core | native_index | 6 | n/a |
| multi_hop | core | none | 6 | n/a |
| multi_hop | core | oracle | 6 | n/a |
| multi_hop | core | pull | 6 | n/a |
| multi_hop | core | push_breadcrumb | 6 | n/a |
| multi_hop | core | push_breadcrumb_pull | 5 | n/a |
| multi_hop | core | push_pages_upper_bound | 6 | n/a |
| multi_hop | core | push_sections_pull | 6 | n/a |
| redundancy | core | native_grep | 8 | n/a |
| redundancy | core | native_index | 8 | n/a |
| redundancy | core | none | 8 | n/a |
| redundancy | core | oracle | 8 | n/a |
| redundancy | core | pull | 8 | n/a |
| redundancy | core | push_breadcrumb | 8 | n/a |
| redundancy | core | push_breadcrumb_pull | 8 | n/a |
| redundancy | core | push_pages_upper_bound | 8 | n/a |
| redundancy | core | push_sections_pull | 8 | n/a |
| single_hop | core | native_grep | 15 | n/a |
| single_hop | core | native_index | 15 | n/a |
| single_hop | core | none | 15 | n/a |
| single_hop | core | oracle | 15 | n/a |
| single_hop | core | pull | 15 | n/a |
| single_hop | core | push_breadcrumb | 15 | n/a |
| single_hop | core | push_breadcrumb_pull | 15 | n/a |
| single_hop | core | push_pages_upper_bound | 15 | n/a |
| single_hop | core | push_sections_pull | 15 | n/a |
| temporal | core | native_grep | 12 | n/a |
| temporal | core | native_index | 12 | n/a |
| temporal | core | none | 12 | n/a |
| temporal | core | oracle | 12 | n/a |
| temporal | core | pull | 12 | n/a |
| temporal | core | push_breadcrumb | 12 | n/a |
| temporal | core | push_breadcrumb_pull | 12 | n/a |
| temporal | core | push_pages_upper_bound | 12 | n/a |
| temporal | core | push_sections_pull | 12 | n/a |
| unprompted_push | core | native_grep | 18 | n/a |
| unprompted_push | core | native_index | 20 | n/a |
| unprompted_push | core | none | 20 | n/a |
| unprompted_push | core | oracle | 20 | n/a |
| unprompted_push | core | pull | 20 | n/a |
| unprompted_push | core | push_breadcrumb | 20 | n/a |
| unprompted_push | core | push_breadcrumb_pull | 20 | n/a |
| unprompted_push | core | push_pages_upper_bound | 20 | n/a |
| unprompted_push | core | push_sections_pull | 20 | n/a |

## Reference tag followed (issue athenaeum#1831, report_only)

`tag_followed_rate` is the share of answers that quoted the `[ref: TAG]` `REFERENCE_TAG_INSTRUCTION` asks for, for every one of the probe's planted tags -- see `tag_followed`. This is the athenaeum#1753 correctness rule, DEMOTED by the operator ruling on athenaeum#1791 comment 5732689494: it feeds no §7 condition and no `correctness_rate` above, which is now graded on `answer_markers` (the planted fact) plus delivered-page evidence, never this tag. `n/a` means no probe in that group carries `answer_tokens` to grade against (including every abstention group).

| probe_class | corpus_scale | arm | n | tag_followed_rate |
| --- | --- | --- | --- | --- |
| abstention | core | native_grep | 6 | n/a |
| abstention | core | native_index | 6 | n/a |
| abstention | core | none | 6 | n/a |
| abstention | core | oracle | 6 | n/a |
| abstention | core | pull | 6 | n/a |
| abstention | core | push_breadcrumb | 6 | n/a |
| abstention | core | push_breadcrumb_pull | 6 | n/a |
| abstention | core | push_pages_upper_bound | 6 | n/a |
| abstention | core | push_sections_pull | 6 | n/a |
| disambiguation | core | native_grep | 7 | 0.571 |
| disambiguation | core | native_index | 7 | 1.000 |
| disambiguation | core | none | 7 | 0.000 |
| disambiguation | core | oracle | 7 | 1.000 |
| disambiguation | core | pull | 7 | 0.286 |
| disambiguation | core | push_breadcrumb | 7 | 0.000 |
| disambiguation | core | push_breadcrumb_pull | 7 | 0.714 |
| disambiguation | core | push_pages_upper_bound | 7 | 0.429 |
| disambiguation | core | push_sections_pull | 7 | 1.000 |
| distractor_robustness | core | native_grep | 4 | 1.000 |
| distractor_robustness | core | native_index | 4 | 0.750 |
| distractor_robustness | core | none | 4 | 0.000 |
| distractor_robustness | core | oracle | 4 | 1.000 |
| distractor_robustness | core | pull | 4 | 1.000 |
| distractor_robustness | core | push_breadcrumb | 4 | 0.000 |
| distractor_robustness | core | push_breadcrumb_pull | 4 | 1.000 |
| distractor_robustness | core | push_pages_upper_bound | 4 | 1.000 |
| distractor_robustness | core | push_sections_pull | 4 | 1.000 |
| follow_through | core | native_grep | 12 | 1.000 |
| follow_through | core | native_index | 12 | 1.000 |
| follow_through | core | none | 12 | 0.000 |
| follow_through | core | oracle | 12 | 1.000 |
| follow_through | core | pull | 12 | 1.000 |
| follow_through | core | push_breadcrumb | 12 | 0.000 |
| follow_through | core | push_breadcrumb_pull | 12 | 0.833 |
| follow_through | core | push_pages_upper_bound | 12 | 0.000 |
| follow_through | core | push_sections_pull | 12 | 1.000 |
| multi_hop | core | native_grep | 3 | 1.000 |
| multi_hop | core | native_index | 6 | 1.000 |
| multi_hop | core | none | 6 | 0.000 |
| multi_hop | core | oracle | 6 | 1.000 |
| multi_hop | core | pull | 6 | 1.000 |
| multi_hop | core | push_breadcrumb | 6 | 0.000 |
| multi_hop | core | push_breadcrumb_pull | 5 | 1.000 |
| multi_hop | core | push_pages_upper_bound | 6 | 0.000 |
| multi_hop | core | push_sections_pull | 6 | 1.000 |
| redundancy | core | native_grep | 8 | 0.625 |
| redundancy | core | native_index | 8 | 1.000 |
| redundancy | core | none | 8 | 0.000 |
| redundancy | core | oracle | 8 | 0.750 |
| redundancy | core | pull | 8 | 0.125 |
| redundancy | core | push_breadcrumb | 8 | 0.000 |
| redundancy | core | push_breadcrumb_pull | 8 | 0.250 |
| redundancy | core | push_pages_upper_bound | 8 | 0.250 |
| redundancy | core | push_sections_pull | 8 | 0.750 |
| single_hop | core | native_grep | 15 | 1.000 |
| single_hop | core | native_index | 15 | 0.800 |
| single_hop | core | none | 15 | 0.000 |
| single_hop | core | oracle | 15 | 1.000 |
| single_hop | core | pull | 15 | 1.000 |
| single_hop | core | push_breadcrumb | 15 | 0.000 |
| single_hop | core | push_breadcrumb_pull | 15 | 1.000 |
| single_hop | core | push_pages_upper_bound | 15 | 0.533 |
| single_hop | core | push_sections_pull | 15 | 1.000 |
| temporal | core | native_grep | 12 | 1.000 |
| temporal | core | native_index | 12 | 0.833 |
| temporal | core | none | 12 | 0.000 |
| temporal | core | oracle | 12 | 1.000 |
| temporal | core | pull | 12 | 0.917 |
| temporal | core | push_breadcrumb | 12 | 0.000 |
| temporal | core | push_breadcrumb_pull | 12 | 0.833 |
| temporal | core | push_pages_upper_bound | 12 | 1.000 |
| temporal | core | push_sections_pull | 12 | 1.000 |
| unprompted_push | core | native_grep | 18 | 1.000 |
| unprompted_push | core | native_index | 20 | 1.000 |
| unprompted_push | core | none | 20 | 0.000 |
| unprompted_push | core | oracle | 20 | 1.000 |
| unprompted_push | core | pull | 20 | 0.800 |
| unprompted_push | core | push_breadcrumb | 20 | 0.000 |
| unprompted_push | core | push_breadcrumb_pull | 20 | 0.800 |
| unprompted_push | core | push_pages_upper_bound | 20 | 0.000 |
| unprompted_push | core | push_sections_pull | 20 | 0.800 |

## Marker miss with delivery (issue athenaeum#1842, report_only)

`marker_miss_with_delivery` is the COUNT of gradable cells in that group where every answer-bearing page WAS delivered to the arm (`correctness_rate`'s delivered-page-evidence clause held) and at least one of those pages still contributed no matching `answer_markers` value to the answer -- see `marker_miss_with_delivery`. It splits a `False` cell into its two causes: a delivery gap (counted here as 0) versus a model miss or a marker-matching gap (counted here as 1). `report_only`: this column feeds no §7 condition, no `compute_verdicts` input and no win/loss field, exactly as `tag_followed_rate` above does not. `n/a` means no row in that group is gradable at all (including every abstention group) -- never a counted zero.

| probe_class | corpus_scale | arm | n | marker_miss_with_delivery |
| --- | --- | --- | --- | --- |
| abstention | core | native_grep | 6 | n/a |
| abstention | core | native_index | 6 | n/a |
| abstention | core | none | 6 | n/a |
| abstention | core | oracle | 6 | n/a |
| abstention | core | pull | 6 | n/a |
| abstention | core | push_breadcrumb | 6 | n/a |
| abstention | core | push_breadcrumb_pull | 6 | n/a |
| abstention | core | push_pages_upper_bound | 6 | n/a |
| abstention | core | push_sections_pull | 6 | n/a |
| disambiguation | core | native_grep | 7 | 0 |
| disambiguation | core | native_index | 7 | 0 |
| disambiguation | core | none | 7 | 0 |
| disambiguation | core | oracle | 7 | 0 |
| disambiguation | core | pull | 7 | 0 |
| disambiguation | core | push_breadcrumb | 7 | 7 |
| disambiguation | core | push_breadcrumb_pull | 7 | 0 |
| disambiguation | core | push_pages_upper_bound | 7 | 0 |
| disambiguation | core | push_sections_pull | 7 | 0 |
| distractor_robustness | core | native_grep | 4 | 1 |
| distractor_robustness | core | native_index | 4 | 0 |
| distractor_robustness | core | none | 4 | 0 |
| distractor_robustness | core | oracle | 4 | 0 |
| distractor_robustness | core | pull | 4 | 0 |
| distractor_robustness | core | push_breadcrumb | 4 | 2 |
| distractor_robustness | core | push_breadcrumb_pull | 4 | 1 |
| distractor_robustness | core | push_pages_upper_bound | 4 | 0 |
| distractor_robustness | core | push_sections_pull | 4 | 0 |
| follow_through | core | native_grep | 12 | 1 |
| follow_through | core | native_index | 12 | 0 |
| follow_through | core | none | 12 | 0 |
| follow_through | core | oracle | 12 | 1 |
| follow_through | core | pull | 12 | 0 |
| follow_through | core | push_breadcrumb | 12 | 0 |
| follow_through | core | push_breadcrumb_pull | 12 | 1 |
| follow_through | core | push_pages_upper_bound | 12 | 0 |
| follow_through | core | push_sections_pull | 12 | 0 |
| multi_hop | core | native_grep | 3 | 3 |
| multi_hop | core | native_index | 6 | 4 |
| multi_hop | core | none | 6 | 0 |
| multi_hop | core | oracle | 6 | 1 |
| multi_hop | core | pull | 6 | 4 |
| multi_hop | core | push_breadcrumb | 6 | 0 |
| multi_hop | core | push_breadcrumb_pull | 5 | 4 |
| multi_hop | core | push_pages_upper_bound | 6 | 0 |
| multi_hop | core | push_sections_pull | 6 | 4 |
| redundancy | core | native_grep | 8 | 2 |
| redundancy | core | native_index | 8 | 2 |
| redundancy | core | none | 8 | 0 |
| redundancy | core | oracle | 8 | 2 |
| redundancy | core | pull | 8 | 3 |
| redundancy | core | push_breadcrumb | 8 | 8 |
| redundancy | core | push_breadcrumb_pull | 8 | 4 |
| redundancy | core | push_pages_upper_bound | 8 | 2 |
| redundancy | core | push_sections_pull | 8 | 2 |
| single_hop | core | native_grep | 15 | 0 |
| single_hop | core | native_index | 15 | 0 |
| single_hop | core | none | 15 | 0 |
| single_hop | core | oracle | 15 | 1 |
| single_hop | core | pull | 15 | 0 |
| single_hop | core | push_breadcrumb | 15 | 13 |
| single_hop | core | push_breadcrumb_pull | 15 | 0 |
| single_hop | core | push_pages_upper_bound | 15 | 7 |
| single_hop | core | push_sections_pull | 15 | 0 |
| temporal | core | native_grep | 12 | 0 |
| temporal | core | native_index | 12 | 0 |
| temporal | core | none | 12 | 0 |
| temporal | core | oracle | 12 | 0 |
| temporal | core | pull | 12 | 1 |
| temporal | core | push_breadcrumb | 12 | 10 |
| temporal | core | push_breadcrumb_pull | 12 | 2 |
| temporal | core | push_pages_upper_bound | 12 | 0 |
| temporal | core | push_sections_pull | 12 | 0 |
| unprompted_push | core | native_grep | 18 | 12 |
| unprompted_push | core | native_index | 20 | 12 |
| unprompted_push | core | none | 20 | 0 |
| unprompted_push | core | oracle | 20 | 11 |
| unprompted_push | core | pull | 20 | 12 |
| unprompted_push | core | push_breadcrumb | 20 | 4 |
| unprompted_push | core | push_breadcrumb_pull | 20 | 13 |
| unprompted_push | core | push_pages_upper_bound | 20 | 2 |
| unprompted_push | core | push_sections_pull | 20 | 8 |

## Follow-through hop delivered (issue athenaeum#1844, report_only)

`follow_hop_rate` is the share of `follow_through` cells in that group where every page named by `corpus.deep_hop_uids` -- the page an arm can only have landed on by FOLLOWING the body `[[wikilink]]`, never by a lexical match on the query -- was delivered to the arm, read from `_delivered_uids` (the grader's own dispatch). It separates the two causes a `False` `correctness_rate` conflates on this class: the arm never reached the second page (counted here as 0) versus it reached the page and the answer still did not carry the fact (counted here as 1, alongside a `marker_miss_with_delivery` of 1). The deep page is DERIVED from the link graph, never read off a fixed `expected_uids` position. `report_only`: this column feeds no §7 condition, no cutoff, no `compute_verdicts` input and no win/loss field, exactly as `marker_miss_with_delivery` above does not. `n/a` for every non-`follow_through` group -- no hop to have followed is a different fact from a rate of zero.

| probe_class | corpus_scale | arm | n | follow_hop_rate |
| --- | --- | --- | --- | --- |
| abstention | core | native_grep | 6 | n/a |
| abstention | core | native_index | 6 | n/a |
| abstention | core | none | 6 | n/a |
| abstention | core | oracle | 6 | n/a |
| abstention | core | pull | 6 | n/a |
| abstention | core | push_breadcrumb | 6 | n/a |
| abstention | core | push_breadcrumb_pull | 6 | n/a |
| abstention | core | push_pages_upper_bound | 6 | n/a |
| abstention | core | push_sections_pull | 6 | n/a |
| disambiguation | core | native_grep | 7 | n/a |
| disambiguation | core | native_index | 7 | n/a |
| disambiguation | core | none | 7 | n/a |
| disambiguation | core | oracle | 7 | n/a |
| disambiguation | core | pull | 7 | n/a |
| disambiguation | core | push_breadcrumb | 7 | n/a |
| disambiguation | core | push_breadcrumb_pull | 7 | n/a |
| disambiguation | core | push_pages_upper_bound | 7 | n/a |
| disambiguation | core | push_sections_pull | 7 | n/a |
| distractor_robustness | core | native_grep | 4 | n/a |
| distractor_robustness | core | native_index | 4 | n/a |
| distractor_robustness | core | none | 4 | n/a |
| distractor_robustness | core | oracle | 4 | n/a |
| distractor_robustness | core | pull | 4 | n/a |
| distractor_robustness | core | push_breadcrumb | 4 | n/a |
| distractor_robustness | core | push_breadcrumb_pull | 4 | n/a |
| distractor_robustness | core | push_pages_upper_bound | 4 | n/a |
| distractor_robustness | core | push_sections_pull | 4 | n/a |
| follow_through | core | native_grep | 12 | 1.000 |
| follow_through | core | native_index | 12 | 1.000 |
| follow_through | core | none | 12 | 0.000 |
| follow_through | core | oracle | 12 | 1.000 |
| follow_through | core | pull | 12 | 1.000 |
| follow_through | core | push_breadcrumb | 12 | 0.000 |
| follow_through | core | push_breadcrumb_pull | 12 | 0.833 |
| follow_through | core | push_pages_upper_bound | 12 | 0.000 |
| follow_through | core | push_sections_pull | 12 | 1.000 |
| multi_hop | core | native_grep | 3 | n/a |
| multi_hop | core | native_index | 6 | n/a |
| multi_hop | core | none | 6 | n/a |
| multi_hop | core | oracle | 6 | n/a |
| multi_hop | core | pull | 6 | n/a |
| multi_hop | core | push_breadcrumb | 6 | n/a |
| multi_hop | core | push_breadcrumb_pull | 5 | n/a |
| multi_hop | core | push_pages_upper_bound | 6 | n/a |
| multi_hop | core | push_sections_pull | 6 | n/a |
| redundancy | core | native_grep | 8 | n/a |
| redundancy | core | native_index | 8 | n/a |
| redundancy | core | none | 8 | n/a |
| redundancy | core | oracle | 8 | n/a |
| redundancy | core | pull | 8 | n/a |
| redundancy | core | push_breadcrumb | 8 | n/a |
| redundancy | core | push_breadcrumb_pull | 8 | n/a |
| redundancy | core | push_pages_upper_bound | 8 | n/a |
| redundancy | core | push_sections_pull | 8 | n/a |
| single_hop | core | native_grep | 15 | n/a |
| single_hop | core | native_index | 15 | n/a |
| single_hop | core | none | 15 | n/a |
| single_hop | core | oracle | 15 | n/a |
| single_hop | core | pull | 15 | n/a |
| single_hop | core | push_breadcrumb | 15 | n/a |
| single_hop | core | push_breadcrumb_pull | 15 | n/a |
| single_hop | core | push_pages_upper_bound | 15 | n/a |
| single_hop | core | push_sections_pull | 15 | n/a |
| temporal | core | native_grep | 12 | n/a |
| temporal | core | native_index | 12 | n/a |
| temporal | core | none | 12 | n/a |
| temporal | core | oracle | 12 | n/a |
| temporal | core | pull | 12 | n/a |
| temporal | core | push_breadcrumb | 12 | n/a |
| temporal | core | push_breadcrumb_pull | 12 | n/a |
| temporal | core | push_pages_upper_bound | 12 | n/a |
| temporal | core | push_sections_pull | 12 | n/a |
| unprompted_push | core | native_grep | 18 | n/a |
| unprompted_push | core | native_index | 20 | n/a |
| unprompted_push | core | none | 20 | n/a |
| unprompted_push | core | oracle | 20 | n/a |
| unprompted_push | core | pull | 20 | n/a |
| unprompted_push | core | push_breadcrumb | 20 | n/a |
| unprompted_push | core | push_breadcrumb_pull | 20 | n/a |
| unprompted_push | core | push_pages_upper_bound | 20 | n/a |
| unprompted_push | core | push_sections_pull | 20 | n/a |

### Weak probes (NONE arm already answers correctly)

Probes where the floor (no context at all) already grades correct — a signal the ground truth leaked into the model's own prior knowledge, or the token is otherwise guessable, not that any retrieval arm helped. Read alongside the ORACLE ceiling: a probe listed here needs a harder token or a different question, not a better arm.

_none observed in this run_

## Index coverage (NATIVE_INDEX only, athenaeum#1725)

`index_coverage = index_lines_loaded / pages` -- the fraction of the corpus the TRUNCATED index still names. `index_lines_loaded` is read back from what Claude Code actually loaded into the session (the `attachment` event in the session transcript), never recomputed from the 200-line/25KB cap: Claude Code performs the truncation, this report only observes its result. `n/a` means no NATIVE_INDEX row in that group carried a readable coverage figure (e.g. auto memory did not activate).

| probe_class | corpus_scale | n | index_coverage |
| --- | --- | --- | --- |
| abstention | core | 6 | 1.000 |
| disambiguation | core | 7 | 1.000 |
| distractor_robustness | core | 4 | 1.000 |
| follow_through | core | 12 | 1.000 |
| multi_hop | core | 6 | 1.000 |
| redundancy | core | 8 | 1.000 |
| single_hop | core | 15 | 1.000 |
| temporal | core | 12 | 1.000 |
| unprompted_push | core | 20 | 1.000 |

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
| disambiguation | n/a |
| distractor_robustness | core |
| follow_through | n/a |
| multi_hop | n/a |
| redundancy | n/a |
| single_hop | n/a |
| temporal | n/a |
| unprompted_push | n/a |

## Frontier (cost vs. quality — never a single composite)

This report intentionally emits no weighted score combining cost and quality: the weights are a business judgement, not a measurement (issue athenaeum#1523). Read the Cost and Query-quality/Utilization sections above side by side, per (probe_class, corpus_scale), and decide the tradeoff from there — the numbers below are restated unweighted, one arm's cost beside its utilization, nothing summed across them.

| probe_class | corpus_scale | arm | mean_input_tokens/turn | mean_output_tokens/turn | uid_citation_rate | distinctive_ngram_overlap |
| --- | --- | --- | --- | --- | --- | --- |
| abstention | core | native_grep | 1221.4 | 93.9 | n/a | n/a |
| abstention | core | native_index | 7896.3 | 90.2 | n/a | n/a |
| abstention | core | none | 280.7 | 47.5 | n/a | n/a |
| abstention | core | oracle | 280.7 | 46.0 | n/a | n/a |
| abstention | core | pull | 4851.9 | 125.1 | 0.000 | 0.000 |
| abstention | core | push_breadcrumb | 519.0 | 157.7 | n/a | 0.069 |
| abstention | core | push_breadcrumb_pull | 4413.9 | 134.0 | 0.000 | 0.002 |
| abstention | core | push_pages_upper_bound | 1489.7 | 71.2 | 0.000 | 0.000 |
| abstention | core | push_sections_pull | 4042.7 | 98.7 | 0.000 | 0.000 |
| disambiguation | core | native_grep | 2334.3 | 179.8 | n/a | n/a |
| disambiguation | core | native_index | 8966.2 | 175.3 | n/a | n/a |
| disambiguation | core | none | 278.6 | 51.9 | n/a | n/a |
| disambiguation | core | oracle | 953.0 | 222.7 | 0.000 | 0.110 |
| disambiguation | core | pull | 3495.1 | 203.0 | 0.086 | 0.057 |
| disambiguation | core | push_breadcrumb | 520.9 | 238.6 | n/a | 0.038 |
| disambiguation | core | push_breadcrumb_pull | 4617.8 | 226.0 | 0.114 | 0.028 |
| disambiguation | core | push_pages_upper_bound | 1529.9 | 260.0 | 0.029 | 0.084 |
| disambiguation | core | push_sections_pull | 3867.9 | 282.1 | 0.000 | 0.055 |
| distractor_robustness | core | native_grep | 2473.2 | 303.3 | n/a | n/a |
| distractor_robustness | core | native_index | 9107.6 | 311.1 | n/a | n/a |
| distractor_robustness | core | none | 273.5 | 71.2 | n/a | n/a |
| distractor_robustness | core | oracle | 790.0 | 124.8 | 0.000 | 0.144 |
| distractor_robustness | core | pull | 4894.2 | 210.4 | 0.000 | 0.028 |
| distractor_robustness | core | push_breadcrumb | 519.5 | 348.5 | n/a | 0.112 |
| distractor_robustness | core | push_breadcrumb_pull | 4520.8 | 288.2 | 0.000 | 0.014 |
| distractor_robustness | core | push_pages_upper_bound | 1442.5 | 344.0 | 0.000 | 0.076 |
| distractor_robustness | core | push_sections_pull | 5720.5 | 231.4 | 0.000 | 0.021 |
| follow_through | core | native_grep | 1502.4 | 98.3 | n/a | n/a |
| follow_through | core | native_index | 8546.9 | 131.0 | n/a | n/a |
| follow_through | core | none | 303.5 | 108.4 | n/a | n/a |
| follow_through | core | oracle | 734.8 | 152.0 | 0.000 | 0.220 |
| follow_through | core | pull | 4024.9 | 140.8 | 0.000 | 0.036 |
| follow_through | core | push_breadcrumb | 564.7 | 299.5 | n/a | 0.059 |
| follow_through | core | push_breadcrumb_pull | 5394.3 | 182.8 | 0.000 | 0.023 |
| follow_through | core | push_pages_upper_bound | 1486.3 | 276.8 | 0.000 | 0.032 |
| follow_through | core | push_sections_pull | 4408.5 | 186.4 | 0.000 | 0.034 |
| multi_hop | core | native_grep | 1912.1 | 114.8 | n/a | n/a |
| multi_hop | core | native_index | 9038.1 | 141.7 | n/a | n/a |
| multi_hop | core | none | 303.0 | 123.5 | n/a | n/a |
| multi_hop | core | oracle | 875.7 | 156.2 | 0.000 | 0.081 |
| multi_hop | core | pull | 5090.0 | 168.0 | 0.000 | 0.010 |
| multi_hop | core | push_breadcrumb | 538.3 | 133.3 | n/a | 0.012 |
| multi_hop | core | push_breadcrumb_pull | 5534.3 | 190.4 | 0.000 | 0.009 |
| multi_hop | core | push_pages_upper_bound | 1442.0 | 259.8 | 0.000 | 0.014 |
| multi_hop | core | push_sections_pull | 5197.2 | 196.6 | 0.000 | 0.010 |
| redundancy | core | native_grep | 1352.3 | 82.9 | n/a | n/a |
| redundancy | core | native_index | 8453.9 | 107.0 | n/a | n/a |
| redundancy | core | none | 281.8 | 56.8 | n/a | n/a |
| redundancy | core | oracle | 808.0 | 71.2 | 0.000 | 0.103 |
| redundancy | core | pull | 3395.4 | 101.8 | 0.025 | 0.053 |
| redundancy | core | push_breadcrumb | 544.5 | 234.5 | n/a | 0.032 |
| redundancy | core | push_breadcrumb_pull | 4043.7 | 127.4 | 0.000 | 0.035 |
| redundancy | core | push_pages_upper_bound | 1469.5 | 79.4 | 0.000 | 0.047 |
| redundancy | core | push_sections_pull | 3464.2 | 107.5 | 0.000 | 0.075 |
| single_hop | core | native_grep | 1637.0 | 138.4 | n/a | n/a |
| single_hop | core | native_index | 8335.6 | 133.6 | n/a | n/a |
| single_hop | core | none | 281.5 | 55.3 | n/a | n/a |
| single_hop | core | oracle | 742.1 | 242.5 | 0.000 | 0.272 |
| single_hop | core | pull | 3646.5 | 171.0 | 0.000 | 0.072 |
| single_hop | core | push_breadcrumb | 531.9 | 243.1 | n/a | 0.026 |
| single_hop | core | push_breadcrumb_pull | 3955.7 | 180.5 | 0.000 | 0.065 |
| single_hop | core | push_pages_upper_bound | 1388.3 | 178.7 | 0.000 | 0.060 |
| single_hop | core | push_sections_pull | 3652.2 | 310.4 | 0.000 | 0.105 |
| temporal | core | native_grep | 1397.6 | 80.9 | n/a | n/a |
| temporal | core | native_index | 8279.7 | 86.8 | n/a | n/a |
| temporal | core | none | 276.8 | 45.2 | n/a | n/a |
| temporal | core | oracle | 505.0 | 62.5 | 0.000 | 0.112 |
| temporal | core | pull | 3939.0 | 209.7 | 0.000 | 0.026 |
| temporal | core | push_breadcrumb | 524.5 | 169.2 | n/a | 0.028 |
| temporal | core | push_breadcrumb_pull | 4329.0 | 226.2 | 0.000 | 0.021 |
| temporal | core | push_pages_upper_bound | 1350.3 | 235.3 | 0.000 | 0.040 |
| temporal | core | push_sections_pull | 3394.1 | 275.9 | 0.000 | 0.060 |
| unprompted_push | core | native_grep | 1928.6 | 192.6 | n/a | n/a |
| unprompted_push | core | native_index | 8873.8 | 209.7 | n/a | n/a |
| unprompted_push | core | none | 310.9 | 84.0 | n/a | n/a |
| unprompted_push | core | oracle | 888.5 | 293.9 | 0.000 | 0.169 |
| unprompted_push | core | pull | 4317.8 | 303.3 | 0.067 | 0.045 |
| unprompted_push | core | push_breadcrumb | 574.2 | 903.5 | n/a | 0.107 |
| unprompted_push | core | push_breadcrumb_pull | 6086.3 | 360.8 | 0.029 | 0.033 |
| unprompted_push | core | push_pages_upper_bound | 1643.5 | 533.9 | 0.250 | 0.046 |
| unprompted_push | core | push_sections_pull | 4603.5 | 291.6 | 0.131 | 0.052 |

## Verdict: do the free dimensions settle the question?

Observed no_call_rate (0.017) and/or the self-vs-topic query-overlap gap (0.012) are non-trivial. The free dimensions show a directional signal on their own — read the per-(probe_class, corpus_scale) tables above for where it concentrates before deciding whether judged scoring is still needed to establish magnitude.
