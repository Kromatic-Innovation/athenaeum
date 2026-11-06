<!-- SPDX-License-Identifier: Apache-2.0 -->
---
title: "Routed section-level content vs pointers plus tool — 2026-11-06"
---

# Routed section-level content vs pointers plus tool — 2026-11-06

Issue athenaeum#2310, under epic athenaeum#2295. A new grid arm,
`push_sections_pull`, injects routed **section-level** content under a stated
byte budget with `recall` and `read_entity` still available, and is compared
with the shipped `push_breadcrumb_pull`. No arm tested this before:
`push_pages_upper_bound` delivers five full pages with no recall tool, so it
scores 0.000 on `follow_through` by construction, and it is an upper-bound
label from athenaeum#1574 rather than a competing design.

**This report reads athenaeum#2312's three conditions against the measured
noise floor and stops there. It does not apply them.** The shipped-default
ruling is athenaeum#2312's, and the rule was fixed on 2026-11-05 before any of
this data existed.

## The arm

| | |
| --- | --- |
| `Arm` value | `push_sections_pull` |
| byte budget | `PUSH_SECTIONS_BYTE_BUDGET = 4800` |
| where the budget comes from | the shipped unprompted-push dial, `config.resolve_push_token_budget`'s 1200 tokens/turn, through the same 4-bytes-per-token estimator this module already uses (`estimate_tokens`) -- not an invented number |
| over-budget policy | sections are packed in retrieval-rank order against one running byte total; the moment the next whole section would exceed the budget, packing stops for that page **and every lower-ranked page**. A page whose body fits goes in whole; one that does not is truncated to its leading whole sections, or dropped if not even its first section fits. No section is ever cut mid-text. |
| routing | `athenaeum.search.get_backend(...).query(...)` -- the same ranking primitive `mcp_server._recall_via_backend` and every real `recall` call use. Section splitting reuses `athenaeum.tiers._split_into_sections`. Neither is reimplemented. |
| tools available | `recall` **and** `read_entity`, so the arm may still PULL |
| grid size | `ALL_ARMS` 8 -> 9, so a (probe, corpus_scale, replicate) group is now 9 cells |

## Provenance, and why it is stated before any number

athenaeum#2312's conditions may only be read against a noise floor measured
under the **same** model, corpus digest and grader revision. Both runs below
carry their model with the data (`RolloutRecord.model`, added by
athenaeum#2309), so this is checked rather than asserted:

| | higher tier | Haiku |
| --- | --- | --- |
| Actions run | [42174388801](https://github.com/Kromatic-Innovation/athenaeum/actions/runs/42174388801) | [42177905668](https://github.com/Kromatic-Innovation/athenaeum/actions/runs/42177905668) |
| model (read off the rows) | `claude-sonnet-5` | `claude-haiku-4-5-20251001` |
| corpus_digest[core] | `3c5ea876688b089e` | `3c5ea876688b089e` |
| grader_revision | `athenaeum#1935` | `athenaeum#1935` |
| search_backend | `vector` | `vector` |
| probes | the 48 `CONDITION_2_ENROLLED` + `unprompted_push` probes | same 48 |
| replicates | 0 and 1 | 0 only |
| rows | 810 of 864 (**partial**, see below); 804 gradable, 6 harness failures | 432 of 432; 427 gradable, 5 harness failures |
| noise floor used | **this run's own**, from its two replicates | athenaeum#2309's, same model + digest + grader |

The digest and grader match athenaeum#2309's measurement exactly, so the Haiku
comparison is readable against that floor. The higher-tier comparison carries
its own floor from its own store, which is the stronger form: floor and
comparison come from one run, one model, one grader.

### The higher-tier run is partial, and these are the missing cells

It aborted at 810 of 864 cells with
`ValueError: response_text: no text block found among 1 content block(s);
block types were ['thinking']` (`src/athenaeum/provider.py:617`). The provider
has no fallback for a **thinking-only** response, which a Sonnet-class model
emits and Haiku does not, so the grid cannot currently run to completion under
any higher-tier model. Filed separately; it is not fixable inside this issue's
blast radius (`provider.py` is designated reasoning surface and a change there
needs its own eval receipt).

What that cost the comparison: one probe absent entirely
(`invoiceref_rename`, `unprompted_push`) and four probes missing one replicate
each (`relay_sync_fix` and `cache_layer_merge`, both `unprompted_push`;
`bramfield_retainer_renewal`, `single_hop`; `surname_is_ambiguous`,
`disambiguation`). `unprompted_push` therefore reads at n=20 probe-replicates
rather than 24. Every figure below is computed over the cells that completed,
and the per-cell coverage is named in
`routed-section-arm-2026-11-06/noise-floor-sonnet.md` section 6 rather than
left as a percentage.

## Result: pooled across all nine classes

| arm | higher tier (`claude-sonnet-5`) | Haiku |
| --- | --- | --- |
| `push_sections_pull` | **0.833** (75/90) | 0.681 (32/47) |
| `oracle` | 0.822 (74/90) | 0.750 (36/48) |
| `pull` | 0.744 (67/90) | 0.638 (30/47) |
| `native_grep` | 0.718 (61/85) | 0.565 (26/46) |
| `native_index` | 0.689 (62/90) | 0.542 (26/48) |
| `push_breadcrumb_pull` (shipped) | 0.663 (59/89) | 0.702 (33/47) |
| `push_pages_upper_bound` | 0.478 (43/90) | 0.417 (20/48) |
| `push_breadcrumb` | 0.067 (6/90) | 0.062 (3/48) |
| `none` | 0.067 (6/90) | 0.062 (3/48) |

Under the higher-tier model the new arm is 0.170 above the shipped arm pooled,
and marginally above the **ceiling** arm. Under Haiku it is 0.021 below the
shipped arm -- which is inside every floor in play, so the honest reading of
the Haiku row is "no difference", not "slightly worse".

That the arm can edge past `oracle` is worth naming rather than hiding: a
ceiling arm is only a ceiling for the delivery it models. `oracle` hands over
whole ground-truth pages; a routed section can be a better-targeted payload
than the page it came from, and (see cost below) it leaves the model's recall
budget intact.

## Reading athenaeum#2312's three conditions

The ruling is read under the higher-tier model, as it specifies. The floor
column is the paired/McNemar MDE for **this exact pairing** -- the new arm
against `push_breadcrumb_pull` on the same probes -- from this run's own two
replicates.

| condition | threshold | observed | reads as |
| --- | --- | --- | --- |
| **1.** beat the shipped arm on `unprompted_push` by more than **2x** the floor | floor 0.271 -> **> 0.542** | **+0.250** (0.600 vs 0.350, n=20) | **not met** |
| **2.** no loss beyond **1x** the floor on any `CONDITION_2_ENROLLED` class | per-class floor, 0.000-0.607 | **no loss in any class** (deltas +0.000 to +0.333) | **met** |
| **3.** cost per correct at most **1.5x** the shipped arm at the same scale | <= 1.50x | **0.25x to 0.93x**, every class | **met** |

Two of three conditions are met and the first is not, so under the rule as
written routed content does **not** replace pointers as the shipped default.
athenaeum#2312 states a null result is a valid outcome; this is one.

**But condition 1's threshold is not reachable by any arm on this corpus, and
that is a fact about the grid rather than about routed content.** The observed
+0.250 is itself *below* 1x the floor (0.271), so this grid cannot even call it
a win -- and athenaeum#2309's positive control shows why: `oracle`, handed the
ground-truth pages verbatim, answers 0.30 and 0.60 of the `unprompted_push`
probes across the two replicates here (0.25-0.50 at Haiku). A class whose
ceiling arm sits near 0.45 cannot exhibit a 0.542 improvement over an arm at
0.350. Reading condition 1 as evidence against routed content would therefore
be a mistake; it is evidence that the class needs either more probes
(athenaeum#2309: 148 probe-replicates for a 0.10 difference, against the 20
achieved here) or a ceiling that reflects what the probes can actually show.

### Condition 2 in full (higher tier)

| probe_class | n | `push_sections_pull` | `push_breadcrumb_pull` | delta | 1x floor |
| --- | --- | --- | --- | --- | --- |
| abstention | 6 | 0.833 | 0.500 | +0.333 | 0.539 |
| disambiguation | 7 | 1.000 | 1.000 | +0.000 | 0.000 |
| distractor_robustness | 4 | 1.000 | 0.750 | +0.250 | 0.607 |
| follow_through | 12 | 1.000 | 0.750 | +0.250 | 0.350 |
| multi_hop | 5 | 0.200 | 0.200 | +0.000 | 0.000 |
| redundancy | 8 | 0.750 | 0.500 | +0.250 | 0.429 |
| single_hop | 15 | 1.000 | 1.000 | +0.000 | 0.000 |
| temporal | 12 | 1.000 | 0.833 | +0.167 | 0.301 |
| (`unprompted_push`, condition 1) | 20 | 0.600 | 0.350 | +0.250 | 0.271 |

No class loses. Every gain is also inside its own floor, so condition 2 passes
on the "no loss" test it actually states, and **none** of the individual gains
is separately measurable at this n. `follow_through` is the one to watch: the
new arm scores 1.000 against the shipped arm's 0.750 while keeping the recall
tool, which is the behaviour `push_pages_upper_bound` structurally cannot have.

At Haiku the same table shows two losses, `follow_through` -0.167 and
`redundancy` -0.250, both inside their athenaeum#2309 floors (0.243 and 0.429)
and both from a single replicate, so neither is a measurable loss either.

### Condition 3 in full

`cost_per_correct` from each render's own "Cost per correct answer" section
(athenaeum#1734, with athenaeum#2305's cache weighting), as a ratio of the new
arm to the shipped arm -- below 1.00 means cheaper per correct answer:

| probe_class | higher tier | Haiku |
| --- | --- | --- |
| abstention | 0.41x | 0.80x |
| disambiguation | 0.40x | 0.62x |
| distractor_robustness | 0.93x | 0.71x |
| follow_through | 0.39x | 0.55x |
| multi_hop | 0.42x | 0.48x |
| redundancy | 0.25x | 0.88x |
| single_hop | 0.39x | 0.48x |
| temporal | 0.35x | 0.64x |
| unprompted_push | 0.37x | 0.57x |

Cheaper per correct answer in **every** class under **both** models, against a
1.5x ceiling. The mechanism is visible in the same tables: the new arm injects
3-5x more bytes than breadcrumbs (e.g. 46,319 vs 13,756 on `unprompted_push`
under the higher tier) and still spends far fewer read tokens per correct
answer, because a pointer the model has to follow costs a `recall` round trip
that delivered content does not.

## Spend

| dispatch | what | pre-flight estimate | actual | cells |
| --- | --- | --- | --- | --- |
| [42174388801](https://github.com/Kromatic-Innovation/athenaeum/actions/runs/42174388801) | higher-tier grid, 2 replicates | $22.16 + $0.23 pre-flight | **$25.40** | 810 of 864 (thinking-only abort) |
| 42174388801 | `eval` job on the same dispatch | n/a | **$0.61** (cited) | n/a |
| [42177905668](https://github.com/Kromatic-Innovation/athenaeum/actions/runs/42177905668) | Haiku grid, 1 replicate | $3.69 + $0.08 pre-flight | **$3.97** | 432 of 432 |
| 42177905668 | `eval` job on the same dispatch | n/a | **$0.61** (cited) | n/a |
| | **total** | | **$30.59** | |
| | **cap** | | $30.00 + $1.50 released by athenaeum#2309 = **$31.50** | |
| | **unspent** | | **$0.91** | |

Grid figures come from `tests/evals/noise_floor.py` over each store's own
persisted `turn_tokens`, priced at the run's own recorded model with the cache
multipliers `athenaeum.models` documents, counting every persisted row
including harness-failed cells. The two `eval`-job figures are not this
dispatch's own receipts: that job runs on every non-`calibration` dispatch of
`evals.yml` and its summary is not reachable from a build lane, so the USD 0.61
measured across three sampled runs in `docs/design/eval-trigger-surface.md` §2
is counted and cited.

The higher-tier run overran its estimate by 15% (the `--dry-run` per-cell token
constant is low, as athenaeum#2309 also found) and was stopped by the
thinking-only error rather than by its ceiling: `--max-tokens 7300000` was set
explicitly, against 7.16M actually consumed.

## Limits

- **One scale.** `core` only, both models. The epic's cutoff-scale question is
  untouched here.
- **The higher-tier comparison is 94% of its grid**, and three of the five
  affected probes are `unprompted_push` -- the class condition 1 reads.
- **The Haiku comparison has one replicate**, so it carries no floor of its
  own and is read against athenaeum#2309's. Its two apparent losses are inside
  that floor and inside single-replicate noise.
- **No arm is retired and no default is changed.** The shipped hook is
  untouched by this issue.
- **`push_sections_pull` beating `oracle` under the higher tier is a 0.011
  pooled difference** -- well inside the floor. It is reported because the
  direction matters for how `oracle` should be read, not as a finding.
